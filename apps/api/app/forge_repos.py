"""The repositories a person's own git connections reach, one read per connected forge root, for the
project and agent forms' Repo URL lookup (`GET /api/git-repos`, `routes/git_connections.py`).

Every listed URL is parsed through `forges.parse_repo_url` before it is returned, so a row the form offers is
one the project save accepts; a row that does not parse is dropped. A root whose read fails is reported as
`failed` and never as "reaches nothing", so the form never warns against a URL it could not check; a root
whose listing hit `_MAX_PAGES` is `truncated` for the same reason.

What each forge lists:
  * GitHub: `/user/repos` — owned, collaborator and org-member repos the person reaches, across every org
    (company) at once, less any org whose OAuth App access restrictions withhold them.
  * GitLab (gitlab.com or self-managed): `/projects?membership=true`.
  * Bitbucket: `/repositories?role=member`, every workspace.
  * Azure DevOps: every organization the token's profile is a member of (`app.vssps.visualstudio.com`), else
    the credential's own test organization; then each organization's git repositories.

`list_owners` reads what a NEW repository can be created in, for the create wizard's "Create a new repository"
(`GET /api/git-repos/owners`): owners with no repository yet included, collaborator-only ones left out.
  * GitHub: the person's login, then `/user/orgs`; a token without `read:org` (an OAuth App grant made before
    `github_oauth.SCOPE` asked for it, or a GitHub App lacking "Members: read") is refused that, so the orgs of
    `/user/repos?affiliation=organization_member` stand in and the source's `detail` says an org with no
    repository is missed and how to fix it (`GITHUB_ORGS_UNREAD`, `GITHUB_APP_ORGS_UNREAD`).
  * GitLab: the person's username (their own namespace), then `/groups?min_access_level=30` (Developer+).
  * Bitbucket: `/user/workspaces`, less any where the person is only a collaborator.
  * Azure DevOps: each organization's projects, as `organization/project`.
The person's own account (GitHub login, GitLab username) is the source's `account`; the picker lists it first.
On GitHub an owner the connection positively cannot create a private repository in is listed `blocked`, with why
(`_github_blocked`): a GitHub App not installed on it (`/user/installations`), or an org whose members may not create
(private) repositories while the person is only a member (`/user/memberships/orgs`, then `/orgs/{org}`). An owner
whose installation lacks "Administration: write" — optional on the App — is a `handoff` instead: the person creates
the repository on github.com with their own rights and Jaah only initializes it. An answer that cannot be read marks
nothing: Create then says what GitHub says.

`default_branch` reads one repository's default branch through the same connection, for the forms'
Integration branch prefill (`GET /api/git-repos/default-branch`); any failure is None, never an error.

No token is logged or returned. The DB reads (selection, renewal) happen before the forge reads, and the
caller releases its transaction before calling `list_repos`.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Optional

import httpx
from sqlalchemy.orm import Session

from app import forges, git_access, git_connections, github_oauth, http_bounded
from app.models.identity import AdminUser

log = logging.getLogger("app.forge_repos")

# One page is each forge's maximum page size; ten pages bound one root at 1,000 repositories.
_PAGE_SIZE = 100
_MAX_PAGES = 10
# Azure DevOps lists repositories per organization; more organizations than this are not read.
_MAX_AZURE_ORGS = 20
# One forge request, and the whole lookup (every root in parallel, at most `_MAX_WORKERS` at once).
_REQUEST_TIMEOUT_SECONDS = 10.0
_BUDGET_SECONDS = 20.0
_MAX_WORKERS = 8
# One page's body: a GitHub page of 100 full repository objects is well under 1 MB.
_MAX_BODY_BYTES = 8 * 1024 * 1024

_AZURE_PROFILE = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1"
_AZURE_ACCOUNTS = "https://app.vssps.visualstudio.com/_apis/accounts"

SourceState = Literal["ok", "failed"]


@dataclass(frozen=True)
class Repo:
    """One repository: its web `url` (what the form saves), `owner` (the org, user, group, workspace or
    Azure organization/project it is grouped under) and `name`."""

    url: str
    forge: str
    root: str
    owner: str
    name: str
    # The self-managed GitLab's https base URL when it lives under a sub-path (the project form's
    # "GitLab base URL"); None everywhere else.
    base_url: Optional[str] = None


@dataclass
class Source:
    """One connected root's read: `ok` with its repositories, or `failed` with a sentence saying why. An `ok`
    read's `detail` names a limit of it (`GITHUB_ORGS_UNREAD`)."""

    root: str
    forge: str
    state: SourceState = "ok"
    detail: Optional[str] = None
    truncated: bool = False
    repos: list[Repo] = field(default_factory=list)
    # `list_owners`' read: spelled as a `Repo.owner` on this root.
    owners: list[str] = field(default_factory=list)
    # The person's own account among `owners`, where the forge has one.
    account: Optional[str] = None
    # Owners listed that cannot be created in, each with a short why (it rides the picker's option label).
    blocked: dict[str, str] = field(default_factory=dict)
    # GitHub owners whose App installation lacks "Administration: write": created on github.com by the person, then
    # initialized through the connection (Contents write suffices). An owner also `blocked` is blocked.
    handoff: set[str] = field(default_factory=set)
    # GitHub's numeric account id of each owner it answered one for: the App's install page opens on that account.
    owner_ids: dict[str, int] = field(default_factory=dict)


class _ReadFailed(RuntimeError):
    """A forge read that did not answer a usable page; the message is shown to the person."""


_TOKEN_REFUSED = "The forge refused this connection's token. Reconnect it in Profile > Git."


@dataclass(frozen=True)
class _Plan:
    """What one root's read needs, resolved from the DB before any forge is dialled."""

    root: str
    forge: str
    login: git_access.GitLogin
    test_org: Optional[str]


def plan_reads(session: Session, user: AdminUser) -> tuple[list[_Plan], list[Source]]:
    """The reads to make for every root `user` has a usable connection for, plus a `failed` source for
    each connected root that cannot be read (needs reconnecting). A root with no connection is neither. DB
    reads only: the private-host check resolves DNS, so it runs inside the read's budget (`_read_root`)."""
    plans: list[_Plan] = []
    failed: list[Source] = []
    for root in git_connections.listed_roots(session, user):
        ref = git_connections.root_ref(root)
        if ref is None:
            continue
        try:
            credential = git_access.resolve_for_user(session, user.id, ref)
        except git_access.GitAccessNeedsReconnect:
            failed.append(Source(root, ref.forge, "failed", "Reconnect this forge in Profile > Git."))
            continue
        except git_access.GitAccessError:
            continue  # not connected
        try:
            login = git_access.rest_login_for(session, credential)
        except git_access.GitAccessError:
            failed.append(Source(root, ref.forge, "failed", "Reconnect this forge in Profile > Git."))
            continue
        test_org = (credential.config or {}).get("test_org")
        plans.append(_Plan(root, ref.forge, login, test_org if isinstance(test_org, str) else None))
    return plans, failed


def list_repos(plans: list[_Plan]) -> list[Source]:
    """Read every planned root in parallel under one `_BUDGET_SECONDS` bound; a root that does not finish
    in time is `failed`. Sources come back in the plans' order, repositories sorted by owner then name."""
    return _read_all(plans, _read_root)


def list_owners(plans: list[_Plan]) -> list[Source]:
    """`list_repos`' parallel read, of the owners a repository can be created in, sorted."""
    return _read_all(plans, _read_owners)


def _read_all(plans: list[_Plan], read: Callable[[_Plan, float], Source]) -> list[Source]:
    if not plans:
        return []
    deadline = time.monotonic() + _BUDGET_SECONDS
    # Not a `with` block: its exit waits for every worker, which would unbound the request.
    pool = ThreadPoolExecutor(max_workers=min(len(plans), _MAX_WORKERS), thread_name_prefix="forge-repos")
    futures = [pool.submit(read, plan, deadline) for plan in plans]
    wait(futures, timeout=_BUDGET_SECONDS + 0.5)
    pool.shutdown(wait=False)
    sources: list[Source] = []
    for plan, future in zip(plans, futures):
        if not future.done():
            sources.append(Source(plan.root, plan.forge, "failed", "The forge did not answer in time."))
            continue
        sources.append(future.result())
    return sources


def _read_root(plan: _Plan, deadline: float) -> Source:
    def collect(client: httpx.Client, source: Source) -> None:
        readers = {"github": _github, "gitlab": _gitlab, "bitbucket": _bitbucket, "azure_devops": _azure_devops}
        seen: set[str] = set()
        for url, truncated in readers[plan.forge](client, plan, deadline):
            source.truncated = source.truncated or truncated
            if url is None:
                continue
            repo = _repo(url, plan)
            if repo is not None and repo.url not in seen:
                seen.add(repo.url)
                source.repos.append(repo)
        source.repos.sort(key=lambda r: (r.owner.lower(), r.name.lower()))
    return _read(plan, collect)


def _read_owners(plan: _Plan, deadline: float) -> Source:
    def collect(client: httpx.Client, source: Source) -> None:
        readers = {
            "github": _github_owners, "gitlab": _gitlab_owners, "bitbucket": _bitbucket_owners,
            "azure_devops": _azure_devops_owners,
        }
        for owner, truncated in readers[plan.forge](client, plan, deadline, source):
            source.truncated = source.truncated or truncated
            if owner is not None and owner not in source.owners:
                source.owners.append(owner)
        source.owners.sort(key=str.lower)
        if plan.forge == "github":
            source.blocked = _github_blocked(client, plan, deadline, source)
    return _read(plan, collect)


def _read(plan: _Plan, collect: Callable[[httpx.Client, Source], None]) -> Source:
    """One root's read through its connection, every failure a `failed` source naming why."""
    source = Source(plan.root, plan.forge)
    try:
        with _plan_client(plan) as client:
            collect(client, source)
    except _ReadFailed as exc:
        return Source(plan.root, plan.forge, "failed", str(exc))
    except httpx.HTTPError as exc:
        log.warning("forge-repos: %s read failed (%s)", plan.root, type(exc).__name__)
        return Source(plan.root, plan.forge, "failed", f"{forges.FORGE_LABELS[plan.forge]} could not be reached.")
    except Exception:  # noqa: BLE001 - one forge's surprise must not hide every other forge's list
        log.exception("forge-repos: %s read raised", plan.root)
        return Source(plan.root, plan.forge, "failed", f"{forges.FORGE_LABELS[plan.forge]} could not be read.")
    return source


def plan_read(session: Session, user: AdminUser, ref: forges.RepoRef) -> Optional[_Plan]:
    """The read for `ref`'s root through `user`'s own connection, or None when it has no usable one. DB
    reads only, as `plan_reads`."""
    try:
        credential = git_access.resolve_for_user(session, user.id, ref)
        login = git_access.rest_login_for(session, credential)
    except git_access.GitAccessError:
        return None
    test_org = (credential.config or {}).get("test_org")
    return _Plan(ref.root, ref.forge, login, test_org if isinstance(test_org, str) else None)


def default_branch(plan: _Plan, ref: forges.RepoRef) -> Optional[str]:
    """`ref`'s default branch as its forge reports it, or None when it cannot be read: the form then keeps
    its own seed, so every failure is quiet."""
    # A `.`/`..` segment in the user's URL would walk the API path to another endpoint: refused, the rest escaped.
    segments = ref.path.split("/")
    if any(part in ("", ".", "..") for part in segments):
        return None
    owner_repo = "/".join(_quote(part) for part in segments)
    deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
    try:
        with _plan_client(plan) as client:
            if plan.forge == "github":
                client.headers["Accept"] = "application/vnd.github+json"
                return _str(_get(client, f"https://api.github.com/repos/{owner_repo}", deadline)[0], "default_branch")
            if plan.forge == "gitlab":
                url = f"https://{plan.root}/api/v4/projects/{_quote(ref.path)}"
                return _str(_get(client, url, deadline)[0], "default_branch")
            if plan.forge == "bitbucket":
                url = f"https://api.bitbucket.org/2.0/repositories/{owner_repo}"
                return _str(_get(client, url, deadline)[0], "mainbranch", "name")
            if len(segments) != 3:
                return None
            org, project, name = (_quote(urllib.parse.unquote(part)) for part in segments)
            body, _headers = _get(client, f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{name}",
                                  deadline, params={"api-version": "7.1"})
            # Azure names the full ref (`refs/heads/main`).
            branch = (_str(body, "defaultBranch") or "").removeprefix("refs/heads/")
            return branch or None
    except (_ReadFailed, httpx.HTTPError):
        return None
    except Exception:  # noqa: BLE001 - a prefill never fails the form
        log.exception("forge-repos: %s default-branch read raised", plan.root)
        return None


def _plan_client(plan: _Plan) -> httpx.Client:
    """`_client` carrying `plan`'s connection. A self-managed GitLab's host is the person's choice: `_ReadFailed`
    when it resolves to a private address, else every request dials an address that check vetted (`_Pinned`, as
    `siem_export.post` does), so a rebinding domain cannot swap in a private address after the check."""
    client = _client(_rest_auth(plan.forge, plan.login))
    host = httpx.URL(f"https://{plan.root}").raw_host.decode("ascii")
    if host in forges.KNOWN_HOSTS:
        return client
    refusal, addresses = http_bounded.vetted_addresses(f"https://{plan.root}")
    if refusal is not None or not addresses:
        client.close()
        raise _ReadFailed("This host cannot be reached from Jaah.")
    client._transport = _Pinned(client._transport, host, addresses)
    # A proxy from the environment would resolve the name itself: none is mounted.
    client._mounts = {}
    return client


class _Pinned(httpx.BaseTransport):
    """Sends every request to `addresses` (the next one only when a connection could not be made), `host` kept as
    the `Host` header and the TLS name; a request for any other host is refused. The name is never resolved again."""

    def __init__(self, inner: httpx.BaseTransport, host: str, addresses: tuple[str, ...]) -> None:
        self._inner, self._host, self._addresses = inner, host, addresses

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.raw_host.decode("ascii") != self._host:
            raise httpx.ConnectError("Only the connection's own host is dialled.", request=request)
        target = request.url
        request.extensions = {**request.extensions, "sni_hostname": self._host}
        for address in self._addresses[:-1]:
            request.url = target.copy_with(host=address)
            try:
                return self._inner.handle_request(request)
            except httpx.ConnectError:
                continue
        request.url = target.copy_with(host=self._addresses[-1])
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def _client(auth: httpx.Auth) -> httpx.Client:
    """The one HTTP client a root's read uses (the tests' seam): `http_bounded`'s policy — no redirects, no
    compressed bodies — carrying the connection's authentication."""
    client = http_bounded.client(_REQUEST_TIMEOUT_SECONDS, {"Accept": "application/json"})
    client.auth = auth
    return client


def _rest_auth(forge: str, login: git_access.GitLogin) -> httpx.Auth:
    """The REST API's authentication for `login`: the REST username's Basic pair where the forge takes one
    (`GitLogin.api_username`), Azure's Basic PAT pair, else a Bearer token."""
    if login.api_username:
        return httpx.BasicAuth(login.api_username, login.token)
    if forge == "azure_devops" and login.scheme == "basic":
        return httpx.BasicAuth(login.username, login.token)
    return _Bearer(login.token)


class _Bearer(httpx.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def _repo(url: str, plan: _Plan) -> Optional[Repo]:
    """The row for a forge-reported web URL, or None when it does not parse as a repo on this root."""
    base_url = f"https://{plan.root}" if "/" in plan.root else None
    self_managed = plan.root not in forges.FIXED_ROOTS
    try:
        ref = forges.parse_repo_url(url, "gitlab" if self_managed else None, base_url)
    except forges.ForgeUrlError:
        return None
    if ref.root != plan.root or ref.forge != plan.forge:
        return None
    owner, _, name = ref.path.rpartition("/")
    return Repo(url=ref.web_url(), forge=ref.forge, root=ref.root, owner=owner, name=name, base_url=base_url)


def _get(
    client: httpx.Client, url: str, deadline: float, *, params: Optional[dict[str, Any]] = None,
) -> tuple[Any, httpx.Headers]:
    """One page's JSON and headers: the body read under `_MAX_BODY_BYTES` and the lookup's wall-clock
    deadline (a per-operation timeout never bounds a drip)."""
    if time.monotonic() >= deadline:
        raise _ReadFailed("The forge did not answer in time.")
    with client.stream("GET", url, params=params) as response:
        # Only a refusal is read as a limit or SSO: a 200 carrying `remaining: 0` was the last allowed call.
        refused = response.status_code != 200
        if refused and (response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0"):
            raise _ReadFailed("The forge is rate-limiting this connection. Try again in a few minutes.")
        if refused and response.headers.get("x-github-sso"):
            raise _ReadFailed("An organization requires SAML sign-in for this token. Authorize it on GitHub.")
        if response.status_code in (401, 403):
            raise _ReadFailed(_TOKEN_REFUSED)
        if response.status_code != 200:
            raise _ReadFailed(f"The forge answered HTTP {response.status_code}.")
        raw, cut = http_bounded.read_capped(response, _MAX_BODY_BYTES, stop=lambda: time.monotonic() >= deadline)
        headers = response.headers
    body = None if cut else http_bounded.json_or_none(raw)
    if body is None:
        raise _ReadFailed("The forge answered something that is not its API.")
    return body, headers


def _str(entry: Any, *path: str) -> Optional[str]:
    value = entry
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value else None


# Each reader yields `(web URL or None, truncated)`; `None` with `True` marks a listing cut at `_MAX_PAGES`.
Rows = Iterator[tuple[Optional[str], bool]]


def _github(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    client.headers["Accept"] = "application/vnd.github+json"
    for page in range(1, _MAX_PAGES + 1):
        body, _headers = _get(client, "https://api.github.com/user/repos", deadline,
                              params={"per_page": _PAGE_SIZE, "page": page, "sort": "full_name"})
        if not isinstance(body, list):
            raise _ReadFailed("GitHub answered something that is not a repository list.")
        for entry in body:
            full_name = _str(entry, "full_name")
            yield (f"https://github.com/{full_name}" if full_name else None), False
        if len(body) < _PAGE_SIZE:
            return
    yield None, True


def _gitlab(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    for page in range(1, _MAX_PAGES + 1):
        body, headers = _get(client, f"https://{plan.root}/api/v4/projects", deadline, params={
            "membership": "true", "simple": "true", "order_by": "path", "sort": "asc",
            "per_page": _PAGE_SIZE, "page": page,
        })
        if not isinstance(body, list):
            raise _ReadFailed("GitLab answered something that is not a project list.")
        for entry in body:
            path = _str(entry, "path_with_namespace")
            yield (f"https://{plan.root}/{path}" if path else None), False
        if not headers.get("x-next-page"):
            return
    yield None, True


def _bitbucket_pages(client: httpx.Client, url: str, params: dict[str, Any], deadline: float, what: str):
    """Every entry of one paged Bitbucket list (`values`), then `(None, True)` when `_MAX_PAGES` cut it short.
    `next` is an absolute link, only ever followed on Bitbucket's own API host."""
    next_params: Optional[dict[str, Any]] = params
    for _page in range(_MAX_PAGES):
        body, _headers = _get(client, url, deadline, params=next_params)
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, list):
            raise _ReadFailed(f"Bitbucket answered something that is not a {what} list.")
        for entry in values:
            yield entry, False
        next_url, next_params = _str(body, "next"), None
        if next_url is None or not next_url.startswith("https://api.bitbucket.org/2.0/"):
            return
        url = next_url
    yield None, True


def _bitbucket(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    # Bitbucket retired the cross-workspace `GET /2.0/repositories` (it answers 404), so each workspace the
    # account belongs to is listed on its own.
    workspaces: list[str] = []
    truncated = False
    for entry, cut in _bitbucket_pages(
        client, "https://api.bitbucket.org/2.0/user/workspaces", {"pagelen": _PAGE_SIZE}, deadline, "workspace",
    ):
        slug = _str(entry, "workspace", "slug") if entry is not None else None
        truncated = truncated or cut
        if slug:
            workspaces.append(slug)
    for workspace in workspaces:
        for entry, cut in _bitbucket_pages(
            client, f"https://api.bitbucket.org/2.0/repositories/{_quote(workspace)}",
            {"role": "member", "pagelen": _PAGE_SIZE, "sort": "full_name"}, deadline, "repository",
        ):
            if cut:
                truncated = True
                continue
            full_name = _str(entry, "full_name")
            yield (f"https://bitbucket.org/{full_name}" if full_name else None), False
    if truncated:
        yield None, True


def _azure_devops(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    orgs = _azure_orgs(client, plan, deadline)
    if not orgs:
        raise _ReadFailed("No Azure DevOps organization is reachable with this connection.")
    truncated = len(orgs) > _MAX_AZURE_ORGS
    read = 0
    listed = 0
    for org in orgs[:_MAX_AZURE_ORGS]:
        try:
            body, _headers = _get(client, f"https://dev.azure.com/{_quote(org)}/_apis/git/repositories",
                                  deadline, params={"api-version": "7.1"})
        except _ReadFailed:
            # One organization's policy can refuse a token the others accept: its repos are unread, not absent.
            truncated = True
            continue
        values = body.get("value") if isinstance(body, dict) else None
        if not isinstance(values, list):
            truncated = True
            continue
        read += 1
        for entry in values:
            if not isinstance(entry, dict):
                continue
            project, name = _str(entry, "project", "name"), _str(entry, "name")
            if entry.get("isDisabled") is True or project is None or name is None:
                continue
            listed += 1
            yield f"https://dev.azure.com/{_quote(org)}/{_quote(project)}/_git/{_quote(name)}", False
    if read == 0:
        raise _ReadFailed("No Azure DevOps organization answered with its repositories.")
    if listed == 0 and not truncated:
        # Azure DevOps answers a Stakeholder (no Repos in private projects) with an empty list, not a refusal: seen
        # on a real org whose projects listed fine. An empty list read in full would also warn every
        # URL "Not available" with no reason, so it is said here instead.
        raise _ReadFailed(
            f"this account sees no repositories in {', '.join(orgs[:_MAX_AZURE_ORGS])}. A Stakeholder sees none; "
            "give it Basic access in the organization's Users settings."
        )
    if truncated:
        yield None, True


def _quote(segment: str) -> str:
    """One URL path segment: a `/` in a forge-supplied name never becomes a path separator."""
    return urllib.parse.quote(segment, safe="")


def _azure_orgs(client: httpx.Client, plan: _Plan, deadline: float) -> list[str]:
    """Every organization the token's profile belongs to; a token scoped to one organization cannot read
    the profile, so its own test organization stands in."""
    try:
        member = _str(_get(client, _AZURE_PROFILE, deadline)[0], "publicAlias")
        if member is not None:
            body, _headers = _get(client, _AZURE_ACCOUNTS, deadline,
                                  params={"memberId": member, "api-version": "7.1"})
            values = body.get("value") if isinstance(body, dict) else None
            names = sorted({n for n in (_str(v, "accountName") for v in values or []) if n}, key=str.lower)
            if names:
                return names
    except _ReadFailed:
        pass
    return [plan.test_org] if plan.test_org else []


# Each owner reader yields `(owner or None, truncated)`, as the repository readers do, and notes the person's own
# account (and any limit of its read) on `source`.

# The `detail` of a GitHub read whose orgs came from the person's repositories (module header): an OAuth App
# grant gains `read:org` by reconnecting; a GitHub App only from its own "Members: read" permission.
GITHUB_ORGS_UNREAD = "Organizations with no repository are missing. Reconnect GitHub in Profile > Git to list them."
GITHUB_APP_ORGS_UNREAD = "Organizations with no repository are missing: the GitHub App lacks \"Members: read\"."


def _github_owners(client: httpx.Client, plan: _Plan, deadline: float, source: Source) -> Rows:
    client.headers["Accept"] = "application/vnd.github+json"
    user = _get(client, "https://api.github.com/user", deadline)[0]
    source.account = _str(user, "login")
    if source.account and isinstance(user.get("id"), int):
        source.owner_ids[source.account] = user["id"]
    yield source.account, False
    try:
        yield from _github_list(client, "https://api.github.com/user/orgs", deadline, {}, "login", ids=source.owner_ids)
    except _ReadFailed as exc:
        # Only a refusal is the missing grant; a timeout or an outage says what it was.
        if str(exc) != _TOKEN_REFUSED:
            source.detail = f"Organizations with no repository are missing: {exc}"
        elif github_oauth.is_app_issued(plan.login.token):
            source.detail = GITHUB_APP_ORGS_UNREAD
        else:
            source.detail = GITHUB_ORGS_UNREAD
        yield from _github_list(client, "https://api.github.com/user/repos", deadline,
                                {"affiliation": "organization_member"}, "owner", "login", ids=source.owner_ids)


# Why `_github_blocked` marks an owner.
GITHUB_APP_NOT_INSTALLED = "the GitHub App is not installed here"
GITHUB_MEMBERS_CANNOT_CREATE = "members may not create repositories here"
GITHUB_MEMBERS_CANNOT_CREATE_PRIVATE = "members may not create private repositories here"
# The orgs whose settings are read, one request each.
_MAX_ORG_SETTINGS = 10


def _github_blocked(client: httpx.Client, plan: _Plan, deadline: float, source: Source) -> dict[str, str]:
    """Each listed owner this connection positively cannot create a private repository in, with why; each one its App
    installation may not create in is added to `source.handoff`. Every read is best-effort: one that fails (a token
    without the permission to read it, an outage) marks nothing."""
    blocked: dict[str, str] = {}
    if github_oauth.is_app_issued(plan.login.token):
        try:
            installations = github_installations(client, deadline)
            unread = False
        except (_ReadFailed, httpx.HTTPError):
            installations, unread = {}, True
        for owner in [] if unread else source.owners:
            installation = installations.get(owner.lower())
            if installation is None:
                blocked[owner] = GITHUB_APP_NOT_INSTALLED
            elif _str(installation, "permissions", "administration") != "write":
                source.handoff.add(owner)
    try:
        members_of = [org for org, role in _github_org_roles(client, deadline).items() if role != "admin"]
    except (_ReadFailed, httpx.HTTPError):
        members_of = []
    listed = {o.lower(): o for o in source.owners}
    unmarked = [listed[o.lower()] for o in members_of if o.lower() in listed and listed[o.lower()] not in blocked]
    for org in unmarked[:_MAX_ORG_SETTINGS]:
        try:
            settings, _headers = _get(client, f"https://api.github.com/orgs/{_quote(org)}", deadline)
        except (_ReadFailed, httpx.HTTPError):
            continue
        # GitHub shows an org's full settings to its owners; to a member these may be absent: unknown, never "may not".
        if isinstance(settings, dict) and settings.get("members_can_create_repositories") is False:
            blocked[org] = GITHUB_MEMBERS_CANNOT_CREATE
        elif isinstance(settings, dict) and settings.get("members_can_create_private_repositories") is False:
            blocked[org] = GITHUB_MEMBERS_CANNOT_CREATE_PRIVATE
    return blocked


def github_installations(client: httpx.Client, deadline: float) -> dict[str, dict[str, Any]]:
    """A GitHub App user token's installations, keyed by the lowercased account login they are on."""
    found: dict[str, dict[str, Any]] = {}
    for page in range(1, _MAX_PAGES + 1):
        body, _headers = _get(client, "https://api.github.com/user/installations", deadline,
                              params={"per_page": _PAGE_SIZE, "page": page})
        entries = body.get("installations") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise _ReadFailed("GitHub answered something that is not an installation list.")
        for entry in entries:
            login = _str(entry, "account", "login")
            if login:
                found[login.lower()] = entry
        if len(entries) < _PAGE_SIZE:
            break
    return found


def _github_org_roles(client: httpx.Client, deadline: float) -> dict[str, str]:
    """The person's active org memberships: org login -> role (`admin`, `member`, `billing_manager`)."""
    roles: dict[str, str] = {}
    for page in range(1, _MAX_PAGES + 1):
        body, _headers = _get(client, "https://api.github.com/user/memberships/orgs", deadline,
                              params={"state": "active", "per_page": _PAGE_SIZE, "page": page})
        if not isinstance(body, list):
            raise _ReadFailed("GitHub answered something that is not a membership list.")
        for entry in body:
            org, role = _str(entry, "organization", "login"), _str(entry, "role")
            if org and role:
                roles[org] = role
        if len(body) < _PAGE_SIZE:
            break
    return roles


def _github_list(client: httpx.Client, url: str, deadline: float, params: dict[str, Any], *path: str,
                 ids: Optional[dict[str, int]] = None) -> Rows:
    """Each entry's `path` value; with `ids`, also notes each value's numeric `id` beside it (the same object's)."""
    for page in range(1, _MAX_PAGES + 1):
        body, _headers = _get(client, url, deadline, params={**params, "per_page": _PAGE_SIZE, "page": page})
        if not isinstance(body, list):
            raise _ReadFailed("GitHub answered something that is not a list.")
        for entry in body:
            value = _str(entry, *path)
            if ids is not None and value:
                parent = entry
                for key in path[:-1]:
                    parent = parent.get(key) if isinstance(parent, dict) else None
                if isinstance(parent, dict) and isinstance(parent.get("id"), int):
                    ids[value] = parent["id"]
            yield value, False
        if len(body) < _PAGE_SIZE:
            return
    yield None, True


def _gitlab_owners(client: httpx.Client, plan: _Plan, deadline: float, source: Source) -> Rows:
    api = f"https://{plan.root}/api/v4"
    source.account = _str(_get(client, f"{api}/user", deadline)[0], "username")
    yield source.account, False
    for page in range(1, _MAX_PAGES + 1):
        body, headers = _get(client, f"{api}/groups", deadline, params={
            "min_access_level": 30, "per_page": _PAGE_SIZE, "page": page,
        })
        if not isinstance(body, list):
            raise _ReadFailed("GitLab answered something that is not a group list.")
        for entry in body:
            yield _str(entry, "full_path"), False
        if not headers.get("x-next-page"):
            return
    yield None, True


def _bitbucket_owners(client: httpx.Client, plan: _Plan, deadline: float, _source: Source) -> Rows:
    for entry, cut in _bitbucket_pages(
        client, "https://api.bitbucket.org/2.0/user/workspaces", {"pagelen": _PAGE_SIZE}, deadline, "workspace",
    ):
        if cut:
            yield None, True
        # A collaborator reaches some repositories in a workspace but may not create one there.
        elif isinstance(entry, dict) and entry.get("permission") != "collaborator":
            yield _str(entry, "workspace", "slug"), False


def _azure_devops_owners(client: httpx.Client, plan: _Plan, deadline: float, _source: Source) -> Rows:
    orgs = _azure_orgs(client, plan, deadline)
    if not orgs:
        raise _ReadFailed("No Azure DevOps organization is reachable with this connection.")
    truncated = len(orgs) > _MAX_AZURE_ORGS
    read = 0
    for org in orgs[:_MAX_AZURE_ORGS]:
        params: dict[str, Any] = {"api-version": "7.1", "$top": _PAGE_SIZE}
        try:
            for _page in range(_MAX_PAGES):
                body, headers = _get(client, f"https://dev.azure.com/{_quote(org)}/_apis/projects", deadline,
                                     params=params)
                values = body.get("value") if isinstance(body, dict) else None
                if not isinstance(values, list):
                    raise _ReadFailed("Azure DevOps answered something that is not a project list.")
                for entry in values:
                    name = _str(entry, "name")
                    yield (f"{_quote(org)}/{_quote(name)}" if name else None), False
                token = headers.get("x-ms-continuationtoken")
                if not token:
                    break
                params = {**params, "continuationToken": token}
            else:
                truncated = True
        except _ReadFailed:
            # One organization's policy can refuse a token the others accept: its projects are unread, not absent.
            truncated = True
            continue
        read += 1
    if read == 0:
        raise _ReadFailed("No Azure DevOps organization answered with its projects.")
    if truncated:
        yield None, True
