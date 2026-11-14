"""The credential-Connect subset of the OAuth provider catalogue — which credential types the control plane runs the
authorization-code flow for, and what each one needs.

The providers themselves (endpoints, scopes, client source, wire quirks) live in `app/oauth_providers.py`; this
module only names which of them fill a credential row (`CredentialTypeSpec.oauth_connect`) and words the button.
A provider either runs on the row's own client (Google, Heroku, Zapier, generic) or on the deployment's app named
by an operator knob (`OAuthProvider.platform_app`: Figma).
"""
from __future__ import annotations

from typing import Optional

from app import oauth_providers

# The integration catalog derives its vendors' types and providers into the registries at import, so it loads
# before this module reads them (`app/integrations/catalog.py`).
from app.integrations import catalog
from app.oauth_providers import PROVIDERS, OAuthProvider

from .registry import CREDENTIAL_TYPES

OAUTH_CONNECT_PROVIDERS: dict[str, OAuthProvider] = {
    key: PROVIDERS[key]
    for key in ("google", "heroku", "zapier", "figma", "generic", "mcp", *sorted(catalog.catalog_provider_keys()))
}


def action_label(provider: OAuthProvider) -> str:
    """The button: "Connect Figma", or a bare "Connect" for the generic provider, whose vendor is the row's."""
    return f"Connect {provider.label}" if provider.label else "Connect"


def provider_label(provider: OAuthProvider) -> Optional[str]:
    """The vendor's name, or None when the provider cannot know it (generic: the route names the authorize host)."""
    return provider.label or None


def connect_for_type(type_slug: str) -> Optional[OAuthProvider]:
    """The control-plane OAuth2 provider that can fill a `type_slug` credential, or None.

    None both for a type with no provider and for an unknown type.
    """
    spec = CREDENTIAL_TYPES.get(type_slug)
    if spec is None or spec.oauth_connect is None:
        return None
    return OAUTH_CONNECT_PROVIDERS.get(spec.oauth_connect)


# Import-time guard: registry `oauth_connect` values must equal `OAUTH_CONNECT_PROVIDERS` keys (equality, not
# subset). An undeclared provider would 500 on every press; an unused one rots. `RuntimeError`, not `assert`, so
# `python -O` keeps it. Additionally, each type must declare the fields its flow reads or fills: the refresh token
# always, the client pair unless the provider runs on the deployment's app, and each endpoint the provider does
# not pin — or the flow resolves a blank at button press.
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
    _needed = {"refresh_token"}
    if oauth_providers.client_source_of(_provider) != "platform":
        _needed |= {"client_id", "client_secret"}
    # A discovery provider's endpoints are derived, never read off the row (`OAuthProvider.discovery`).
    if _provider.authorize_url is None and _provider.discovery is None:
        _needed.add("authorize_url")
    if _provider.token_url is None and _provider.discovery is None:
        _needed.add("token_url")
    if not _needed <= _keys:
        raise RuntimeError(
            f"{_type_slug}: declares oauth_connect={_type_spec.oauth_connect!r} but has no "
            f"{sorted(_needed - _keys)} field(s) - the flow would have nothing to read or fill"
        )
