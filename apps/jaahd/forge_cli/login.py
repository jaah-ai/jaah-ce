"""The forge, its API and the token — from the credential broker's `git_credential`, and nowhere else.

The broker answers with the pinned git-token wire (`apps/api/app/routes/agent/task_secrets.py::GitTokenOut`) for the
task the CALLING PROCESS belongs to; outside a runner it refuses, and that refusal is printed verbatim
(exit 4). The login is held in memory for this one invocation: nothing is written anywhere.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import git_credential_helper as broker  # apps/jaahd/git_credential_helper.py — the one broker client

from .model import CredentialRefused
from .transport import host_of

FORGES = ("github", "gitlab", "bitbucket", "azure_devops")
FORGE_LABELS = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket", "azure_devops": "Azure DevOps"}
# Bitbucket's OAuth git username: that token is a Bearer token to the REST API.
_BITBUCKET_OAUTH_USER = "x-token-auth"


# The longest `write_problem` printed: the CP's sentence names a repository, the credentials tried and a link.
_WRITE_PROBLEM_CAP = 1000


@dataclass(frozen=True)
class Login:
    forge: str
    scheme: str
    username: str
    token: str
    host: str
    api_base: str
    repo_path: str
    web_url: str
    # The REST API's Basic username where it is not git's (the broker's additive `api_username`): the
    # Atlassian account email for a Bitbucket API token. `""` otherwise.
    api_username: str = ""
    # The control plane's sentence naming why no credential of the task starter's can write here (the
    # broker's additive `write_problem`), appended to a refused write. `""` when it can, or cannot tell.
    write_problem: str = ""

    @property
    def label(self) -> str:
        return FORGE_LABELS[self.forge]

    def auth_header(self) -> str:
        """The REST Authorization header. GitHub and GitLab take their git password as a Bearer token;
        Bitbucket does for an OAuth token, and an API token as Basic with the account email; Azure DevOps
        takes a PAT as Basic."""
        if self.scheme == "bearer" or self.forge in ("github", "gitlab") or (
                self.forge == "bitbucket" and self.username == _BITBUCKET_OAUTH_USER):
            return f"Bearer {self.token}"
        username = self.username
        if self.forge == "bitbucket":
            if not self.api_username:
                raise CredentialRefused(
                    "the broker's Bitbucket API-token login carries no account email for the REST API "
                    "— is the control plane older than the api_username wire?")
            username = self.api_username
        return "Basic " + base64.b64encode(f"{username}:{self.token}".encode()).decode()


def same_site(api_host: str, host: str) -> bool:
    """True when the API host is the repo host or a subdomain of it (`api.github.com` for `github.com`,
    `api.bitbucket.org` for `bitbucket.org`) — stricter than a registrable-domain match, which is what
    keeps a login from ever pointing its token at a stranger."""
    return bool(api_host) and bool(host) and (api_host == host or api_host.endswith("." + host))


def from_wire(value: dict) -> Login:
    """Validate the broker's answer into a `Login`, or `CredentialRefused`."""
    def field(key: str) -> str:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw:
            raise CredentialRefused(
                f"the broker's git login carries no {key!r} — is the control plane older than the per-forge wire?")
        return raw

    forge = field("forge")
    if forge not in FORGES:
        raise CredentialRefused(f"the broker's git login names an unsupported forge {forge!r}")
    host = field("host").lower()
    api_base = field("api_base")
    api_host = host_of(api_base)
    if not same_site(api_host, host):
        raise CredentialRefused(
            f"the broker's git login points its API at {api_host or api_base!r}, which is not on {host!r} — refusing")
    web_url = field("web_url")
    if not same_site(host_of(web_url), host):
        raise CredentialRefused(f"the broker's git login names a web URL off {host!r} — refusing")
    token = field("token")
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        raise CredentialRefused("the broker's git login carries a token that is not a printable header value")
    return Login(
        forge=forge, scheme=field("scheme"), username=str(value.get("username") or ""), token=token,
        host=host, api_base=api_base, repo_path=field("repo_path").strip("/"), web_url=web_url.rstrip("/"),
        api_username=str(value.get("api_username") or ""),
        write_problem=" ".join(str(value.get("write_problem") or "").split())[:_WRITE_PROBLEM_CAP],
    )


def fetch(environ, checkout: "str | None" = None, *, fresh: bool = False) -> Login:
    """The login for the repository of the checkout at `checkout` (the CLI's working directory). `fresh`
    reads past the daemon's cache, after the forge refused the cached login (`Client`'s reauth)."""
    try:
        value = broker.ask_broker(broker.socket_path(environ), checkout, fresh=fresh)
    except broker.Refused as refused:
        raise CredentialRefused(str(refused)) from None
    return from_wire(value)


def report_rejected(environ, login: Login) -> None:
    """Tell the control plane the forge refused `login` even after a fresh read (`Client._report_rejected`)."""
    broker.report_forge_auth(broker.socket_path(environ), broker.FORGE_AUTH_REJECTED, login.host, login.repo_path)
