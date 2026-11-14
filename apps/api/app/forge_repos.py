"""The repositories a person's own git connections reach, one read per connected account, for the
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

A root with several connected accounts is read once per account; the lookup lists a repository once (the
default account's read first). The owners each account reaches are recorded (`remember_owners`) when it is
connected (`refresh_owners`) and before a create or initialize whose owner no account is known to reach
(`learn_owner`) — never by a GET — and resolution matches a repository's owner against them
(`git_access.pick_connection`).

No token is logged or returned. The DB reads (selection, renewal) happen before the forge reads, and the
caller releases its transaction before calling `list_repos`.
"""
from __future__ import annotations

import functools
import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forges, git_access, git_connections, github_api, github_oauth, http_bounded, paging
from app.models.credentials import UserGitConnection
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
# A connect's own owners read (`refresh_owners`): the person is waiting on the save.
_REFRESH_BUDGET_SECONDS = 8.0
_MAX_WORKERS = 8
# One page's body: a GitHub page of 100 full repository objects is well under 1 MB.
_MAX_BODY_BYTES = 8 * 1024 * 1024

_AZURE_PROFILE = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1"
_AZURE_ACCOUNTS = "https://app.vssps.visualstudio.com/_apis/accounts"
_AZURE_VERSION = {"api-version": "7.1"}
# Where each forge's repository object names its default branch (`default_branch`).
_DEFAULT_BRANCH = {
    "github": ("default_branch",), "gitlab": ("default_branch",), "bitbucket": ("mainbranch", "name"),
    "azure_devops": ("defaultBranch",),
}

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
    # The connected account (credential) this read went through; None for a `failed` source made from the DB.
    credential_id: Optional[UUID] = None


class ForgeError(RuntimeError):
    """A forge request that did not answer (`request`, `_plan_client`); the message is shown to the person.
    `status`: the forge's refusal, if any. `forge_ci` and `forge_repo_init` raise and catch this same class."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Wording:
    """How one caller words `request`'s failures: fixed sentences, and `refused(status, said)` for a status outside
    `ok` that is neither a limit nor an SSO demand — `said` is the forge's own message when its host is one of
    `ECHOED_HOSTS`, else None. `unreadable` None: a body that is not JSON (or none at all) comes back as None."""

    timeout: str
    rate_limited: str
    sso: str
    refused: Callable[[int, Optional[str]], str]
    unreadable: Optional[str] = None


_TOKEN_REFUSED = "The forge refused this connection's token. Reconnect it in Profile > Git."
_WORDING = Wording(
    timeout="The forge did not answer in time.",
    rate_limited="The forge is rate-limiting this connection. Try again in a few minutes.",
    sso="An organization requires SAML sign-in for this token. Authorize it on GitHub.",
    refused=lambda status, said: _TOKEN_REFUSED if status in (401, 403) else f"The forge answered HTTP {status}.",
    unreadable="The forge answered something that is not its API.",
)
# The forges whose own words are passed on (`Wording.refused`'s `said`): a self-managed host is the person's choice,
# and its answer must not carry what some other address said (`http_bounded`'s rebinding residual).
ECHOED_HOSTS = frozenset({"api.github.com", "gitlab.com", "api.bitbucket.org", "dev.azure.com"})


@dataclass(frozen=True)
class _Plan:
    """What one root's read needs, resolved from the DB before any forge is dialled."""

    root: str
    forge: str
    login: git_access.GitLogin
    test_org: Optional[str]
    credential_id: Optional[UUID] = None


def _plan_for(
    session: Session, user: AdminUser, ref: forges.RepoRef, credential_id: Optional[UUID]
) -> _Plan | Source | None:
    """The read through `credential_id` (resolution's own pick when None) on `ref`'s root: a `_Plan`, a `failed`
    source when the account needs reconnecting, or None when the root is not connected."""
    failed = Source(ref.root, ref.forge, "failed", "Reconnect this forge in Profile > Git.", credential_id=credential_id)
    try:
        credential = git_access.resolve_for_user(session, user.id, ref, credential_id=credential_id)
    except git_access.GitAccessNeedsReconnect:
        return failed
    except git_access.GitAccessError:
        return None  # not connected
    try:
        login = git_access.rest_login_for(session, credential)
    except git_access.GitAccessError:
        return failed
    test_org = (credential.config or {}).get("test_org")
    return _Plan(ref.root, ref.forge, login, test_org if isinstance(test_org, str) else None, credential.id)


def plan_reads(session: Session, user: AdminUser) -> tuple[list[_Plan], list[Source]]:
    """The reads to make for every account `user` connected (each root's default first), plus a `failed` source
    for each one that cannot be read (needs reconnecting). A root with no connection is neither. DB reads only:
    the private-host check resolves DNS, so it runs inside the read's budget (`_read_root`)."""
    plans: list[_Plan] = []
    failed: list[Source] = []
    for root in git_connections.listed_roots(session, user):
        ref = git_connections.root_ref(root)
        if ref is None:
            continue
        rows = git_access.root_connections(session, user, root)
        for credential_id in [row.credential_id for row in rows] or [None]:
            planned = _plan_for(session, user, ref, credential_id)
            if isinstance(planned, _Plan):
                plans.append(planned)
            elif planned is not None:
                failed.append(planned)
    return plans, failed


def unique_repos(sources: list[Source]) -> list[Repo]:
    """Every source's repositories, each URL once: two accounts on one root often reach the same one."""
    seen: set[str] = set()
    repos: list[Repo] = []
    for source in sources:
        for repo in source.repos:
            if repo.url not in seen:
                seen.add(repo.url)
                repos.append(repo)
    return repos


def remember_owners(session: Session, user: AdminUser, sources: list[Source]) -> None:
    """Record, on each connection an `ok` source read through, the owners its account reaches: the owners read
    (`list_owners`) or the owners of the repositories read (`list_repos`), with the account itself. A truncated
    read adds to what was recorded instead of replacing it. Commits; best effort — a failed write is logged."""
    try:
        for source in sources:
            if source.state != "ok" or source.credential_id is None:
                continue
            seen = {*source.owners, *(repo.owner for repo in source.repos), *([source.account] if source.account else [])}
            row = session.scalars(select(UserGitConnection).where(
                UserGitConnection.user_id == user.id, UserGitConnection.credential_id == source.credential_id,
            )).first()
            if row is None:
                continue
            owners = {o.lower() for o in seen if o}
            if source.truncated:
                owners |= set(row.owners or [])
            row.owners = sorted(owners)
        session.commit()
    except Exception:  # noqa: BLE001 - recording owners never fails the read that found them
        session.rollback()
        log.exception("forge-repos: owners for user=%s not recorded", user.id)


def learn_owner(session: Session, user: AdminUser, ref: forges.RepoRef) -> None:
    """Before a write to `ref` (create, initialize): when `user` holds several accounts on its root and none is
    recorded as reaching its owner, read and record every account's owners (`remember_owners`, in parallel under
    `_REFRESH_BUDGET_SECONDS`), so the write goes through the account that reaches it. An owner joined after the
    account was connected is learned here. Best effort; releases the caller's transaction when it reads."""
    try:
        rows = git_access.root_connections(session, user, ref.root)
        if len(rows) < 2 or not ref.path or git_access.owner_known(session, rows, ref.path):
            return
        plans = [p for p in (_plan_for(session, user, ref, row.credential_id) for row in rows) if isinstance(p, _Plan)]
    except Exception:  # noqa: BLE001 - learning owners never fails the write it precedes
        log.exception("forge-repos: owners read for user=%s not planned", user.id)
        return
    session.rollback()
    deadline = time.monotonic() + _REFRESH_BUDGET_SECONDS
    remember_owners(session, user, [_read_owners(plan, deadline) for plan in plans])


def refresh_owners(session: Session, user: AdminUser, root: str, credential_id: UUID) -> None:
    """Read and record (`remember_owners`) the owners the account `credential_id` reaches on `root`, right after
    it was connected, so resolution can route its repositories to it at once. Bounded by
    `_REFRESH_BUDGET_SECONDS`; any failure leaves the owners as they were. Releases the caller's transaction."""
    try:
        ref = git_connections.root_ref(root)
        planned = _plan_for(session, user, ref, credential_id) if ref is not None else None
    except Exception:  # noqa: BLE001 - the connect is committed; recording owners never fails it
        log.exception("forge-repos: owners read for user=%s not planned", user.id)
        planned = None
    session.rollback()
    if not isinstance(planned, _Plan):
        return
    remember_owners(session, user, [_read_owners(planned, time.monotonic() + _REFRESH_BUDGET_SECONDS)])


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
    for plan, future in zip(plans, futures, strict=True):
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
    """One account's read through its connection, every failure a `failed` source naming why."""
    source = Source(plan.root, plan.forge, credential_id=plan.credential_id)
    try:
        with _plan_client(plan) as client:
            collect(client, source)
    except ForgeError as exc:
        return Source(plan.root, plan.forge, "failed", str(exc), credential_id=plan.credential_id)
    except httpx.HTTPError as exc:
        log.warning("forge-repos: %s read failed (%s)", plan.root, type(exc).__name__)
        return Source(plan.root, plan.forge, "failed", f"{forges.FORGE_LABELS[plan.forge]} could not be reached.",
                      credential_id=plan.credential_id)
    except Exception:  # noqa: BLE001 - one forge's surprise must not hide every other forge's list
        log.exception("forge-repos: %s read raised", plan.root)
        return Source(plan.root, plan.forge, "failed", f"{forges.FORGE_LABELS[plan.forge]} could not be read.",
                      credential_id=plan.credential_id)
    return source


def plan_read(session: Session, user: AdminUser, ref: forges.RepoRef) -> Optional[_Plan]:
    """The read for `ref`'s root through `user`'s own connection, or None when it has no usable one. DB
    reads only, as `plan_reads`."""
    planned = _plan_for(session, user, ref, None)
    return planned if isinstance(planned, _Plan) else None


def default_branch(plan: _Plan, ref: forges.RepoRef) -> Optional[str]:
    """`ref`'s default branch as its forge reports it, or None when it cannot be read: the form then keeps
    its own seed, so every failure is quiet."""
    segments = path_segments(ref.path)
    if segments is None:
        return None
    try:
        with _plan_client(plan) as client:
            body = _get(client, repo_api(plan, segments), time.monotonic() + _REQUEST_TIMEOUT_SECONDS,
                        params=_AZURE_VERSION if plan.forge == "azure_devops" else None)[0]
        # Azure names the full ref (`refs/heads/main`).
        return (_str(body, *_DEFAULT_BRANCH[plan.forge]) or "").removeprefix("refs/heads/") or None
    except (ForgeError, httpx.HTTPError):
        return None
    except Exception:  # noqa: BLE001 - a prefill never fails the form
        log.exception("forge-repos: %s default-branch read raised", plan.root)
        return None


def _plan_client(plan: _Plan) -> httpx.Client:
    """`_client` carrying `plan`'s connection. A self-managed GitLab's host is the person's choice: `ForgeError`
    when it resolves to a private address, else every request dials an address that check vetted (`http_bounded.pin`, as
    `siem_export.post` does), so a rebinding domain cannot swap in a private address after the check."""
    client = _client(_rest_auth(plan.forge, plan.login))
    if plan.forge == "github":
        client.headers["Accept"] = "application/vnd.github+json"
    host = httpx.URL(f"https://{plan.root}").raw_host.decode("ascii")
    if host in forges.KNOWN_HOSTS:
        return client
    refusal, addresses = http_bounded.vetted_addresses(f"https://{plan.root}")
    if refusal is not None or not addresses:
        client.close()
        raise ForgeError("This host cannot be reached from Jaah.")
    return http_bounded.pin(client, host, addresses)


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


def parse_url(url: str, plan: _Plan) -> forges.RepoRef:
    """A repository URL as `plan`'s root reads it (a self-managed root is a GitLab, under its sub-path when it has
    one); `forges.ForgeUrlError` when it does not parse. The caller checks the ref is on `plan`'s own root."""
    base_url = f"https://{plan.root}" if "/" in plan.root else None
    return forges.parse_repo_url(url, None if plan.root in forges.FIXED_ROOTS else "gitlab", base_url)


def path_segments(path: str, least: int = 2) -> Optional[list[str]]:
    """`path`'s segments, decoded (a URL keeps Azure's percent-encoding), or None when there are fewer than `least`
    or one decodes to nothing, `.`, `..` (so `%2e%2e` counts) or holds a `/`: it would walk the API path to another
    endpoint. Each is quoted again (`forges.quote_segment`) where it goes into a request."""
    segments = [urllib.parse.unquote(part) for part in path.split("/")]
    if len(segments) < least or any(part in ("", ".", "..") or "/" in part for part in segments):
        return None
    return segments


def repo_api(plan: _Plan, segments: list[str]) -> str:
    """The repository's own API URL on `plan`'s forge, `segments` its decoded path (`path_segments`)."""
    quoted = [forges.quote_segment(part) for part in segments]
    if plan.forge == "gitlab":
        return f"https://{plan.root}/api/v4/projects/{forges.quote_segment('/'.join(segments))}"
    if plan.forge == "azure_devops":
        if len(segments) != 3:
            raise ForgeError("An Azure DevOps repository URL names its organization, project and repository.")
        org, project, name = quoted
        return f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{name}"
    base = f"{github_api.GITHUB_API_BASE}/repos" if plan.forge == "github" else "https://api.bitbucket.org/2.0/repositories"
    return f"{base}/{'/'.join(quoted)}"


def _repo(url: str, plan: _Plan) -> Optional[Repo]:
    """The row for a forge-reported web URL, or None when it does not parse as a repo on this root."""
    try:
        ref = parse_url(url, plan)
    except forges.ForgeUrlError:
        return None
    if ref.root != plan.root or ref.forge != plan.forge:
        return None
    owner, _, name = ref.path.rpartition("/")
    return Repo(url=ref.web_url(), forge=ref.forge, root=ref.root, owner=owner, name=name,
                base_url=f"https://{plan.root}" if "/" in plan.root else None)


def request(
    client: httpx.Client, method: str, url: str, deadline: float, wording: Wording, *, ok: tuple[int, ...] = (200,),
    json: Any = None, data: Optional[dict[str, str]] = None, files: Any = None, params: Optional[dict[str, Any]] = None,
) -> tuple[Any, httpx.Headers]:
    """One request's JSON body and headers: the body read under `_MAX_BODY_BYTES` and the caller's wall-clock
    `deadline` (a per-operation timeout never bounds a drip); a status outside `ok` is `ForgeError` worded by
    `wording`, carrying the status (a limit or an SSO demand carries none: neither is about the request made)."""
    if time.monotonic() >= deadline:
        raise ForgeError(wording.timeout)
    with client.stream(method, url, json=json, data=data, files=files, params=params) as response:
        status, headers = response.status_code, response.headers
        raw, cut = http_bounded.read_capped(response, _MAX_BODY_BYTES, stop=lambda: time.monotonic() >= deadline)
        echo = response.request.url.host in ECHOED_HOSTS
    body = None if cut else http_bounded.json_or_none(raw)
    if status in ok:
        # A read needs its body (`unreadable` is set): a drip cut at the deadline is a timeout. A write got
        # its answer already — a cut body is None, never a failure for a repository the forge just made.
        if wording.unreadable is not None:
            if cut and time.monotonic() >= deadline:
                raise ForgeError(wording.timeout)
            if body is None:
                raise ForgeError(wording.unreadable)
        return body, headers
    # Only a refusal is read as a limit or SSO: a 200 carrying `remaining: 0` was the last allowed call.
    if status == 429 or headers.get("x-ratelimit-remaining") == "0":
        raise ForgeError(wording.rate_limited)
    if headers.get("x-github-sso"):
        raise ForgeError(wording.sso)
    raise ForgeError(wording.refused(status, _message(body) if echo else None), status)


def _message(body: Any) -> Optional[str]:
    """The forge's own sentence from an error body, bounded."""
    if not isinstance(body, dict):
        return None
    for path in (("message",), ("error", "message"), ("error",), ("errors",)):
        value: Any = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
        if isinstance(value, list) and value:
            return "; ".join(str(v.get("message", v)) if isinstance(v, dict) else str(v) for v in value)[:300]
        if isinstance(value, dict) and value:
            return "; ".join(f"{k}: {v}" for k, v in value.items())[:300]
    return None


def _get(
    client: httpx.Client, url: str, deadline: float, *, params: Optional[dict[str, Any]] = None,
) -> tuple[Any, httpx.Headers]:
    """One page's JSON and headers (`request`, the lookup's wording)."""
    return request(client, "GET", url, deadline, _WORDING, params=params)


def _str(entry: Any, *path: str) -> Optional[str]:
    value = entry
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value else None


# Each reader yields `(web URL or None, truncated)`; `None` with `True` marks a listing cut at `_MAX_PAGES`.
Rows = Iterator[tuple[Optional[str], bool]]


class _Cut(Exception):
    """`_pages`' overflow (`paging.follow_pages`): the listing goes on past `_MAX_PAGES`."""


def _pages(fetch: Callable[[Optional[str]], tuple[list[Any], object]]) -> Iterator[tuple[Any, bool]]:
    """Every entry of the listing `fetch` pages (`paging.follow_pages`), then `(None, True)` when `_MAX_PAGES` cut it
    short. `_str` passes `(None, True)` through: a reader that stops at the cap needs no branch."""
    try:
        for entries in paging.follow_pages(fetch, max_pages=_MAX_PAGES, overflow=_Cut):
            yield from ((entry, False) for entry in entries)
    except _Cut:
        yield None, True


def _each_entry(
    client: httpx.Client, url: str, deadline: float, params: dict[str, Any], *, refusal: str,
    key: Optional[str] = None, header_paged: bool = False,
) -> Iterator[tuple[Any, bool]]:
    """`_pages` over one GitHub or GitLab list, the token the next page's number: GitHub says there is one with a full
    page, GitLab (`header_paged`) with an `x-next-page` header. The body is the list, or an object holding it under
    `key`; anything else raises `ForgeError(refusal)`."""
    def fetch(token: Optional[str]) -> tuple[list[Any], object]:
        page = int(token or 1)
        body, headers = _get(client, url, deadline, params={**params, "per_page": _PAGE_SIZE, "page": page})
        entries = body if key is None else body.get(key) if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise ForgeError(refusal)
        more = headers.get("x-next-page") if header_paged else len(entries) >= _PAGE_SIZE
        return entries, str(page + 1) if more else None
    return _pages(fetch)


def _github(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    for entry, cut in _each_entry(client, f"{github_api.GITHUB_API_BASE}/user/repos", deadline, {"sort": "full_name"},
                                  refusal="GitHub answered something that is not a repository list."):
        full_name = _str(entry, "full_name")
        yield (f"https://github.com/{full_name}" if full_name else None), cut


def _gitlab(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    for entry, cut in _each_entry(
        client, f"https://{plan.root}/api/v4/projects", deadline,
        {"membership": "true", "simple": "true", "order_by": "path", "sort": "asc"},
        refusal="GitLab answered something that is not a project list.", header_paged=True,
    ):
        path = _str(entry, "path_with_namespace")
        yield (f"https://{plan.root}/{path}" if path else None), cut


def _bitbucket_pages(client: httpx.Client, url: str, params: dict[str, Any], deadline: float, what: str):
    """`_pages` over one Bitbucket list (`values`), the token its `next`: an absolute link, only ever followed on
    Bitbucket's own API host."""
    def fetch(token: Optional[str]) -> tuple[list[Any], object]:
        body, _headers = _get(client, token or url, deadline, params=None if token else params)
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, list):
            raise ForgeError(f"Bitbucket answered something that is not a {what} list.")
        next_url = _str(body, "next")
        return values, next_url if next_url and next_url.startswith("https://api.bitbucket.org/2.0/") else None
    return _pages(fetch)


def _bitbucket(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    # Bitbucket retired the cross-workspace `GET /2.0/repositories` (it answers 404), so each workspace the
    # account belongs to is listed on its own. Either listing's cut row rides through: `_read_root` keeps the flag.
    workspaces: list[str] = []
    for entry, cut in _bitbucket_pages(
        client, "https://api.bitbucket.org/2.0/user/workspaces", {"pagelen": _PAGE_SIZE}, deadline, "workspace",
    ):
        slug = _str(entry, "workspace", "slug")
        if slug:
            workspaces.append(slug)
        elif cut:
            yield None, True
    for workspace in workspaces:
        for entry, cut in _bitbucket_pages(
            client, f"https://api.bitbucket.org/2.0/repositories/{forges.quote_segment(workspace)}",
            {"role": "member", "pagelen": _PAGE_SIZE, "sort": "full_name"}, deadline, "repository",
        ):
            full_name = _str(entry, "full_name")
            yield (f"https://bitbucket.org/{full_name}" if full_name else None), cut


def _azure_devops(client: httpx.Client, plan: _Plan, deadline: float) -> Rows:
    orgs = _azure_orgs(client, plan, deadline)
    truncated = False
    listed = 0
    for org, entry, cut in _azure_each(client, orgs, deadline, "git/repositories", _AZURE_VERSION, "repository", "repositories"):
        truncated = truncated or cut
        if org is None or not isinstance(entry, dict):
            continue
        project, name = _str(entry, "project", "name"), _str(entry, "name")
        if entry.get("isDisabled") is True or project is None or name is None:
            continue
        listed += 1
        yield f"https://dev.azure.com/{forges.quote_segment(org)}/{forges.quote_segment(project)}/_git/{forges.quote_segment(name)}", False
    if listed == 0 and not truncated:
        # Azure DevOps answers a Stakeholder (no Repos in private projects) with an empty list, not a refusal: seen
        # on a real org whose projects listed fine. An empty list read in full would also warn every
        # URL "Not available" with no reason, so it is said here instead.
        raise ForgeError(
            f"this account sees no repositories in {', '.join(orgs[:_MAX_AZURE_ORGS])}. A Stakeholder sees none; "
            "give it Basic access in the organization's Users settings."
        )
    if truncated:
        yield None, True


def _azure_each(
    client: httpx.Client, orgs: list[str], deadline: float, path: str, params: dict[str, Any], what: str, many: str,
) -> Iterator[tuple[Optional[str], Any, bool]]:
    """Every entry of `https://dev.azure.com/{org}/_apis/{path}` (`_azure_page`) across `orgs` (`_azure_orgs`), each
    as `(org, entry, cut)`, then `(None, None, True)` when more than `_MAX_AZURE_ORGS` were named or one was unread.
    `what` names one entry (the "not a … list" refusal), `many` the listing (the "no organization answered" one)."""
    if not orgs:
        raise ForgeError("No Azure DevOps organization is reachable with this connection.")
    truncated = len(orgs) > _MAX_AZURE_ORGS
    read = 0
    for org in orgs[:_MAX_AZURE_ORGS]:
        url = f"https://dev.azure.com/{forges.quote_segment(org)}/_apis/{path}"
        try:
            for entry, cut in _pages(functools.partial(_azure_page, client, url, deadline, params, what)):
                yield org, entry, cut
        except ForgeError:
            # One organization's policy can refuse a token the others accept: its rows are unread, not absent.
            truncated = True
            continue
        read += 1
    if read == 0:
        raise ForgeError(f"No Azure DevOps organization answered with its {many}.")
    if truncated:
        yield None, None, True


def _azure_page(
    client: httpx.Client, url: str, deadline: float, params: dict[str, Any], what: str, token: Optional[str],
) -> tuple[list[Any], object]:
    """One page of an Azure DevOps list (`value`) for `_pages`; its `x-ms-continuationtoken` header names the next."""
    body, headers = _get(client, url, deadline, params={**params, "continuationToken": token} if token else params)
    values = body.get("value") if isinstance(body, dict) else None
    if not isinstance(values, list):
        raise ForgeError(f"Azure DevOps answered something that is not a {what} list.")
    return values, headers.get("x-ms-continuationtoken")


def _azure_orgs(client: httpx.Client, plan: _Plan, deadline: float) -> list[str]:
    """Every organization the token's profile belongs to; a token scoped to one organization cannot read
    the profile, so its own test organization stands in."""
    try:
        member = _str(_get(client, _AZURE_PROFILE, deadline)[0], "publicAlias")
        if member is not None:
            body, _headers = _get(client, _AZURE_ACCOUNTS, deadline,
                                  params={"memberId": member, **_AZURE_VERSION})
            values = body.get("value") if isinstance(body, dict) else None
            names = sorted({n for n in (_str(v, "accountName") for v in values or []) if n}, key=str.lower)
            if names:
                return names
    except ForgeError:
        pass
    return [plan.test_org] if plan.test_org else []


# Each owner reader yields `(owner or None, truncated)`, as the repository readers do, and notes the person's own
# account (and any limit of its read) on `source`.

# The `detail` of a GitHub read whose orgs came from the person's repositories (module header): an OAuth App
# grant gains `read:org` by reconnecting; a GitHub App only from its own "Members: read" permission.
GITHUB_ORGS_UNREAD = "Organizations with no repository are missing. Reconnect GitHub in Profile > Git to list them."
GITHUB_APP_ORGS_UNREAD = "Organizations with no repository are missing: the GitHub App lacks \"Members: read\"."


def _github_owners(client: httpx.Client, plan: _Plan, deadline: float, source: Source) -> Rows:
    user = _get(client, f"{github_api.GITHUB_API_BASE}/user", deadline)[0]
    source.account = _str(user, "login")
    if source.account and isinstance(user.get("id"), int):
        source.owner_ids[source.account] = user["id"]
    yield source.account, False
    try:
        yield from _github_list(client, f"{github_api.GITHUB_API_BASE}/user/orgs", deadline, {}, "login", ids=source.owner_ids)
    except ForgeError as exc:
        # Only a refusal is the missing grant; a timeout or an outage says what it was.
        if str(exc) != _TOKEN_REFUSED:
            source.detail = f"Organizations with no repository are missing: {exc}"
        elif github_oauth.is_app_issued(plan.login.token):
            source.detail = GITHUB_APP_ORGS_UNREAD
        else:
            source.detail = GITHUB_ORGS_UNREAD
        yield from _github_list(client, f"{github_api.GITHUB_API_BASE}/user/repos", deadline,
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
        except (ForgeError, httpx.HTTPError):
            installations, unread = {}, True
        for owner in [] if unread else source.owners:
            installation = installations.get(owner.lower())
            if installation is None:
                blocked[owner] = GITHUB_APP_NOT_INSTALLED
            elif _str(installation, "permissions", "administration") != "write":
                source.handoff.add(owner)
    try:
        members_of = [org for org, role in _github_org_roles(client, deadline).items() if role != "admin"]
    except (ForgeError, httpx.HTTPError):
        members_of = []
    listed = {o.lower(): o for o in source.owners}
    unmarked = [listed[o.lower()] for o in members_of if o.lower() in listed and listed[o.lower()] not in blocked]
    for org in unmarked[:_MAX_ORG_SETTINGS]:
        try:
            settings, _headers = _get(client, f"{github_api.GITHUB_API_BASE}/orgs/{forges.quote_segment(org)}", deadline)
        except (ForgeError, httpx.HTTPError):
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
    for entry, _ in _each_entry(client, f"{github_api.GITHUB_API_BASE}/user/installations", deadline, {}, key="installations",
                                refusal="GitHub answered something that is not an installation list."):
        login = _str(entry, "account", "login")
        if login:
            found[login.lower()] = entry
    return found


def _github_org_roles(client: httpx.Client, deadline: float) -> dict[str, str]:
    """The person's active org memberships: org login -> role (`admin`, `member`, `billing_manager`)."""
    roles: dict[str, str] = {}
    for entry, _ in _each_entry(client, f"{github_api.GITHUB_API_BASE}/user/memberships/orgs", deadline, {"state": "active"},
                                refusal="GitHub answered something that is not a membership list."):
        org, role = _str(entry, "organization", "login"), _str(entry, "role")
        if org and role:
            roles[org] = role
    return roles


def _github_list(client: httpx.Client, url: str, deadline: float, params: dict[str, Any], *path: str,
                 ids: Optional[dict[str, int]] = None) -> Rows:
    """Each entry's `path` value; with `ids`, also notes each value's numeric `id` beside it (the same object's)."""
    for entry, cut in _each_entry(client, url, deadline, params,
                                  refusal="GitHub answered something that is not a list."):
        value = _str(entry, *path)
        if ids is not None and value:
            parent = entry
            for key in path[:-1]:
                parent = parent.get(key) if isinstance(parent, dict) else None
            if isinstance(parent, dict) and isinstance(parent.get("id"), int):
                ids[value] = parent["id"]
        yield value, cut


def _gitlab_owners(client: httpx.Client, plan: _Plan, deadline: float, source: Source) -> Rows:
    api = f"https://{plan.root}/api/v4"
    source.account = _str(_get(client, f"{api}/user", deadline)[0], "username")
    yield source.account, False
    for entry, cut in _each_entry(client, f"{api}/groups", deadline, {"min_access_level": 30}, header_paged=True,
                                  refusal="GitLab answered something that is not a group list."):
        yield _str(entry, "full_path"), cut


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
    # A cut row (`org` None) rides through as `(None, True)`: `_read_owners` keeps the flag once set.
    for org, entry, cut in _azure_each(client, _azure_orgs(client, plan, deadline), deadline, "projects",
                                       {**_AZURE_VERSION, "$top": _PAGE_SIZE}, "project", "projects"):
        name = _str(entry, "name")
        yield (f"{forges.quote_segment(org)}/{forges.quote_segment(name)}" if org and name else None), cut
