"""Connect GitHub's own surroundings: its registered callback path, the GitHub App's install hop, and the Git tab's
per-org reach (`/reach`).

Connect GitHub runs on the shared Connect engine (`routes/integrations.py`, with the forges' hooks in
`routes/forge_connect.py` and the row in `forge_oauth.on_grant` → `git_connections.connect_github`, shared with a
GitHub sign-in). GitHub's app keeps `/api/git-connect/callback` as its registered redirect URI, so that route hands
the callback to the engine (`integrations.finish_hooked_callback`). After a GitHub App connect, a person whose account reaches no
installation is carried to the App's install page (`needs_installation`), where GitHub lets a non-owner request an
owner's approval; `/setup` brings them back to the page the attempt came from (`RETURN_PATHS`).
Nothing logs or echoes a secret; browser-visible failures are slugs from the closed `_ERRORS`, the one vocabulary of
every forge's Git return. `app/user_activity.py` excludes `/api/git-connect` from audit capture (the callback's query
carries a live `code`), and `apps/web/nginx.conf.template` keeps it out of nginx logs.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, NamedTuple, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app import forges, git_access, github_api, oauth_flow_cookie
from app import db_async
from app.oauth_flow_cookie import FlowCookie
from app.authz.catalog import PROJECTS_VIEW_KEY, Tenancy
from app.authz.declarations import always, feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.db_async import DbDep
from app.authz.visibility import scoped_project_select
from app.models.identity import AdminUser
from app.models.projects import Project
from app.routes.integrations import HookedCallbackQuery, finish_hooked_callback

log = logging.getLogger("app.git_connect")

router = APIRouter(tags=["git-connect"])

# The root every GitHub OAuth token authenticates (`git_connections.root_ref`).
_GITHUB_ROOT = forges.RepoRef(forge="github", host="github.com", root="github.com", path="")

# The prefix every forge's Connect callback sits under: the flow cookie's path (`forge_connect.CONNECT_HOOKS`).
FLOW_COOKIE_PATH = "/api/git-connect"
# GitHub's registered callback (its app predates the per-forge paths).
CALLBACK_PATH = "/api/git-connect/callback"
# See `git_connect_setup`.
SETUP_MISTYPED_PATH = "/api/git-connect/callback/api/git-connect/setup"

# The console pages a connect may return to: Profile's Git tab (the default) and the popup the create wizard
# opens for a hand-off owner, which tells its opener and closes.
# A closed allow-list, never a free-form target: anything else returns to Profile, so `return_to` cannot be
# an open redirect.
RETURN_PATHS = frozenset({"/profile", "/git-connect/done"})
DEFAULT_RETURN = "/profile"

# Where the attempt was asked to come back to, for the App's install hop: the flow cookie is cleared at the
# callback, and GitHub lands the browser on `/setup` afterwards with no `state` it echoes. Set by
# `install_redirect`, read back through `_return_to`; same path scope and ten-minute life as a flow cookie.
_RETURN = FlowCookie(
    suffix="_git_connect_return",
    path=FLOW_COOKIE_PATH,
    salt="git-connect-return",
    members=("return_to",),
)
RETURN_COOKIE_NAME = _RETURN.name

# An installations read's budget: the person is waiting on a redirect.
_INSTALLATIONS_TIMEOUT_SECONDS = 15.0


# The closed vocabulary of everything a forge Connect's browser is told on its way back to the Git tab: no
# forge's error text reaches the redirect, and each slug names a distinct fix. The engine's own refusals
# (`routes/credentials/connect.py::_CONNECT_ERRORS`) a forge attempt can meet are members too.
_ERRORS = oauth_flow_cookie.connect_errors(
    "the forge",
    bad_request="this connection cannot be made as it stands",
    credential_gone="the connection could not be stored for this person",
    name_taken="another credential took the connection's name during the attempt",
    no_refresh_token="the forge returned no refresh token",
    consent_blocked=(
        "the Microsoft Entra tenant will not let this app sign in until a tenant administrator consents to "
        "it, once for the whole tenant (or the account is outside the app's tenant) — ask one to grant "
        "consent, or paste a personal access token instead"
    ),
    wrong_app_kind=(
        "the credential designated as the GitHub App holds an OAuth App, or the reverse — an operator must fix "
        "the setting"
    ),
    install_requested=(
        "the GitHub App installation still needs an organization owner's approval, so this connection "
        "cannot reach that organization's repositories yet"
    ),
)


class GitReachOwner(BaseModel):
    """One GitHub owner behind a project the caller may view, and whether their connection reaches it.

    Only owners GitHub answered about plainly are listed (`_classify`, `_installation_reach`); anything else is
    omitted, never reported as lost access."""

    owner: str
    reach: Literal["ok", "approval_required", "sso_required", "install_required"]


class GitReachOut(BaseModel):
    """Whether the caller's GitHub connection reaches the orgs behind their projects, for the Git tab.

    `state` is `ok` (GitHub answered, `owners` meaningful) or `unknown` (render nothing: no app, no github.com
    connection, a token that is unreadable or needs reconnecting, or GitHub unreadable). `unknown` is the
    fail-safe: an outage must never paint "you lost access" across every user. For an OAuth App token,
    `approve_url` is GitHub's page where a user requests an org's approval of the app or authorizes it for SAML
    SSO. For a GitHub App token, `install_url` is the App's install page, where an owner installs it and anyone
    else requests that; `installed_count == 0` is the "reaches nothing" headline."""

    state: Literal["ok", "unknown"]
    approve_url: Optional[str] = None
    install_url: Optional[str] = None
    installed_count: Optional[int] = None
    owners: list[GitReachOwner] = []


# A factory, not a constant: `owners` would be shared mutable state across requests.
def _reach_unknown() -> GitReachOut:
    return GitReachOut(state="unknown")


def allowed_return(return_to: Optional[str]) -> str:
    """`return_to` when it is one of `RETURN_PATHS`, else `DEFAULT_RETURN`."""
    return return_to if return_to in RETURN_PATHS else DEFAULT_RETURN


def _return_to(request: Request) -> str:
    """The page this browser's `/start` asked to come back to; Profile when there is no valid cookie."""
    stored = _RETURN.read(request)
    return allowed_return(stored["return_to"] if stored else None)


def return_redirect(return_to: str, *, error: Optional[str] = None, **tags: str) -> RedirectResponse:
    """`oauth_flow_cookie.finish_redirect` to `return_to`, re-checked against `RETURN_PATHS`; `tags` name the forge."""
    return oauth_flow_cookie.finish_redirect(allowed_return(return_to), _ERRORS, error=error, **tags)


def joined_user(session: Session, user: AdminUser) -> AdminUser:
    """`user` as `session`'s own row, for a handler body on a worker thread (`db_async.run_blocking_in_session`):
    a write to it (the Git Access pointer, `git_connections.set_connection`) then lands with that session's
    commit, as on the sync route's one session. An acting operator's seat copy (`act_as.seat_user`) stays
    detached, as it was there."""
    return user if sa_inspect(user).detached else session.merge(user, load=False)


@router.get(CALLBACK_PATH)
@feature("credentials", "create", own_credential=True)
async def git_connect_callback(
    request: Request,
    db: DbDep,
    # `query.error` is `access_denied` when the user declines at GitHub — an ordinary outcome.
    query: HookedCallbackQuery = Depends(HookedCallbackQuery),
    caller: AsyncCaller = Depends(current_caller),
) -> RedirectResponse:
    """GitHub's registered redirect target, finished by the shared engine (`integrations.finish_hooked_callback`):
    the attempt's checks before the code is spent, the exchange, the row (`forge_oauth.on_grant`), then the
    installation carry-forward (`forge_connect.CONNECT_HOOKS`)."""
    return await finish_hooked_callback(request, db, slug="github", **vars(query), user=caller.actor, principal=caller.principal)


# ── The GitHub App's installation carry-forward (see this module's header) ──────────────────────────
#
# GitHub's "Request user authorization (OAuth) during installation" must never be enabled: its redirect
# carries only `client_id`, bypassing PKCE and CSRF. So the callback decides whether to send the browser
# to the install screen, and `/setup` is where GitHub lands it afterwards.

# A GitHub App slug: lowercase alphanumerics and hyphens. A security bound: `slug` is operator-edited
# text interpolated into a URL path, and `.`/`?`/`#` survive quoting, so an unbounded slug could aim the
# hop at an arbitrary github.com path (e.g. an OAuth authorize screen) while the user is primed to approve.
_APP_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?")


def needs_installation(
    session: Session, slug: str, login: Optional[str], access_token: str,
) -> bool:
    """Whether this user should be carried to the App's install screen before they are done.

    Only when the chosen account has no installation of its own AND either it owns a console-managed repo
    or the token reaches no installation at all (a valid token with zero repository reach). A user who
    already reaches repos through an org's installation is not sent: demanding coverage of every account
    would nag them about one they may not want to install on; the Git tab names an org still
    missing it (`/reach`).

    False on every failure (bad or absent slug, no login, unreadable installations), since the credential
    is already committed. `_installation_accounts` returns `None` for unreadable so an outage never sends
    every user to the picker."""
    if install_url(slug) is None:
        # The slug is required on the credential type, so this is an old or hand-edited row. Not echoed.
        log.warning("git-connect: the designated GitHub App has no usable `slug` — skipping the installation "
                    "carry-forward. Set it on the App credential in Directory > Secrets.")
        return False
    if not login:
        # The account read already warned; this only records the consequence.
        return False
    account = login.lower()
    managed = _managed_owner_accounts(session)
    # Release the transaction before the network hop, or the connection idles in transaction through
    # the GitHub read (`app/db.py` records that as a cause of ALB kills).
    session.rollback()
    installed = _installation_accounts(access_token)
    if installed is None or account in installed.accounts:
        return False
    return account in managed or installed.count == 0


class Installations(NamedTuple):
    """What one `/user/installations` read said: which accounts, and how many installations.

    `count` is not `len(accounts)`: an installation without a usable `login` (enterprise accounts) still
    counts, so a parse failure never becomes a claim about reach."""

    accounts: set[str]
    count: int


def _installation_accounts(
    access_token: str, *, timeout: float = _INSTALLATIONS_TIMEOUT_SECONDS,
) -> Optional[Installations]:
    """What a GitHub App user token can see, or `None` when the answer could not be read at all.

    Suspended installations (a truthy `suspended_at`) count as absent: no repository access. One page of 100
    (GitHub's maximum), the budget `timeout` covers; an account past it re-prompts on every connect."""
    try:
        active = [entry for entry in github_api.paged(access_token, "/user/installations", "installations",
                                                      timeout=timeout, max_pages=1, stop_at_cap=True)
                  if not entry.get("suspended_at")]
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("git-connect: could not read installations (%s)", github_api.reason(exc))
        return None
    # `count` is not GitHub's `total_count`, which includes suspended installations.
    return Installations({login for login in map(github_api.account_login, active) if login}, len(active))


def _managed_owner_accounts(session: Session) -> set[str]:
    """The lowercased set of GitHub account owners behind every console Project's `repo_url`.

    Only decides the carry-forward and never leaves the server; what reaches a browser is
    `_visible_owner_repos`, grant-scoped."""
    owners: set[str] = set()
    for repo_url in session.scalars(select(Project.repo_url)).all():
        owner_repo = forges.github_repo_path(repo_url)
        if owner_repo:
            owners.add(owner_repo.split("/", 1)[0].lower())
    return owners


def install_url(slug: str) -> Optional[str]:
    """The designated App's own installation-picker URL, or None when `slug` is not one to build from.

    The one home of that URL and its `_APP_SLUG` guard, for both the carry-forward and `/reach`. None, not a
    raise: both callers have a do-nothing arm."""
    if not _APP_SLUG.fullmatch(slug):
        return None
    return f"https://github.com/apps/{slug}/installations/new"


def clear_return(response: RedirectResponse) -> None:
    """Retire the install hop's return cookie (`_RETURN`) on `response`."""
    _RETURN.clear(response)


def install_redirect(slug: str, return_to: str) -> RedirectResponse:
    """303 to the designated App's own installation-picker screen (303 as in `return_redirect`), keeping the page
    to come back to for `/setup` (`_RETURN`).

    The slug is already validated by `needs_installation`; a None here is a programming error, so it
    raises."""
    url = install_url(slug)
    if url is None:  # pragma: no cover - `needs_installation` refuses the slug before this is reached
        raise ValueError("install_redirect was handed a slug _APP_SLUG refuses")
    response = RedirectResponse(url, status_code=303)
    _RETURN.set(response, {"return_to": allowed_return(return_to)})
    return response


@router.get("/api/git-connect/setup")
# The App's Setup URL saved as the Callback URL with this path appended. Prod's was, and every install
# then 404'd. Serving it too means that one paste error cannot strand a user.
@router.get(SETUP_MISTYPED_PATH)
@always(Tenancy.BOOTSTRAP)
async def git_connect_setup(
    request: Request,
    # GitHub's "Setup URL" params are attacker-controllable; declared like `/callback`'s (defaulted
    # `str`) and never trusted, logged or looked up.
    installation_id: str = Query(default=""),
    setup_action: str = Query(default=""),
) -> RedirectResponse:
    """Where GitHub lands the browser after the installation picker (the App's "Setup URL").

    Both params are attacker-controllable. `setup_action` only chooses between two of our own slugs:
    `request` means a non-owner asked an org owner to approve, so saying "connected" would be false; a forged
    value can at worst show one of two accurate-or-cautious sentences. `installation_id` is never read.

    No flow cookie (it was cleared at the callback; nothing to verify); the return cookie `install_redirect` set
    names the page to land on, allow-listed (`_return_to`), and is retired here. `@always`: the response carries
    nothing about anyone."""
    return_to = _return_to(request)
    response = return_redirect(return_to, error="install_requested" if setup_action == "request" else None)
    _RETURN.clear(response)
    return response


# ── The Git tab's org reach (see `GitReachOut`) ────────────────────────────────────────────────────

# Shorter than `_INSTALLATIONS_TIMEOUT_SECONDS`: this sync handler holds a threadpool worker on a repeatable GET,
# and the tab renders nothing when it cannot tell.
_REACH_TIMEOUT_SECONDS = 5.0

# Owners probed per view, one request each, in parallel: bounds the fan-out an authenticated caller drives.
_REACH_MAX_OWNERS = 10

# GitHub's 403 messages, matched lowercased and never echoed: an org's OAuth App access restrictions, and
# an org's SAML SSO enforcement that this token has not been authorized for.
_APPROVAL_REQUIRED_MARKER = "oauth app access restrictions"
_SSO_REQUIRED_MARKER = "saml"

# GitHub's own name charsets: an owner is alphanumerics and hyphens; a repository adds `.` and `_`. `.` and `..`
# alone are refused separately: quoted or not, they would walk the probe's API path.
_OWNER_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")

# What a probe that got no answer at all returns, apart from "answered, but nothing we can name".
_UNREADABLE = "unreadable"


def _visible_owner_repos(session: Session, principal: Optional[Principal]) -> dict[str, str]:
    """One `owner/repo` per lowercased GitHub owner behind the projects this principal may view, the
    alphabetically first repo of each.

    Through `scoped_project_select` (`routes/portfolio.py`'s rule): it is returned to a browser under a
    `credentials` declaration, so it must not enumerate the org's GitHub accounts past what they may see."""
    stmt = scoped_project_select(principal, key=PROJECTS_VIEW_KEY).with_only_columns(Project.repo_url)
    repos: dict[str, str] = {}
    for repo_url in session.scalars(stmt).all():
        owner_repo = forges.github_repo_path(repo_url)
        if not owner_repo or not _probe_safe(owner_repo):
            continue
        owner = owner_repo.split("/", 1)[0].lower()
        if owner not in repos or owner_repo.lower() < repos[owner].lower():
            repos[owner] = owner_repo
    return repos


def _probe_safe(owner_repo: str) -> bool:
    """Whether `owner/repo` is two names in GitHub's charsets, neither `.` nor `..`: a project's URL is
    operator text, and the probe interpolates it into an API path sent with the user's token."""
    owner, _, repo = owner_repo.partition("/")
    return (
        bool(_OWNER_NAME.fullmatch(owner)) and bool(_REPO_NAME.fullmatch(repo)) and repo not in (".", "..")
    )


def _connected_token(session: Session, user: AdminUser) -> Optional[str]:
    """The token behind this user's github.com connection, renewed when due (`git_access.token_for`: a GitHub
    App's user token usually expires within 8h), or None for every case with nothing to probe: nothing
    selected, a dead grant, another forge's type, an unreadable row."""
    try:
        credential = git_access.resolve_for_user(session, user.id, _GITHUB_ROOT)
    except git_access.GitAccessError:
        return None
    if credential.type != "github":
        return None
    try:
        return git_access.token_for(session, credential)
    except git_access.GitAccessError:
        # Includes `GitAccessNeedsReconnect`; the connection's own row prompts the reconnect.
        return None


def _classify(status: int, payload: Any) -> Optional[str]:
    """A probe's answer as a reach, or None when it says nothing we can name (a 404 is also how GitHub
    hides a repo from a token that cannot see it, so it is never read as lost access)."""
    if status == 200:
        return "ok"
    if status != 403 or not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if not isinstance(message, str):
        return None
    lowered = message.lower()
    if _APPROVAL_REQUIRED_MARKER in lowered:
        return "approval_required"
    if _SSO_REQUIRED_MARKER in lowered:
        return "sso_required"
    return None


def _probe(token: str, owner_repo: str) -> Optional[str]:
    """`GET /repos/{owner}/{repo}` with the user's token: `_classify`'s answer, or `_UNREADABLE` when GitHub
    could not be reached or is rate limiting the token. Only a status or an exception class is logged."""
    try:
        status, payload = github_api.repo_json(token, owner_repo, timeout=_REACH_TIMEOUT_SECONDS)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("git-connect: reach probe failed (%s)", github_api.reason(exc))
        return _UNREADABLE
    return _classify(status, payload)


@router.get("/api/git-connect/reach", response_model=GitReachOut)
@feature("credentials", "view", own_credential=True)
@db_async.on_thread(actor=lambda caller, **_: caller.actor)
def git_connect_reach(
    session: Session,
    # For the owner list (project data, grant-scoped), not the credential; see `_visible_owner_repos`.
    caller: AsyncCaller = Depends(current_caller),
) -> GitReachOut:
    """Which GitHub orgs behind the caller's projects their connection reaches, and which an org's OAuth
    App access restrictions, SAML SSO or a missing GitHub App installation withholds, for the Git tab (see
    `GitReachOut`). The connection's own token says which app made it (`git_access.token_app_kind`), so a
    connection from the app not chosen now is still described truthfully.

    DB reads first, then the transaction is released before the GitHub hop so no connection idles in
    transaction (`app/db.py`). Takes no user id; `own_credential=True` over `credentials:view` makes it
    self-service, and `resolve_for_user` re-authorizes the selection. Nothing is written or cached: an org's
    policy and installations change on github.com with no event we see."""
    # The token may be renewed inside its read (`git_access.token_for`), the app setting is read on a session of
    # its own, and the probes are GitHub HTTP: a worker thread, never the loop.
    token = _connected_token(session, caller.actor)
    if token is None:
        return _reach_unknown()
    try:
        app = git_access.app_config(kind=git_access.token_app_kind(token), open_client_secret=False)
    except git_access.GitAccessError:
        # The app that made this connection is no longer designated: nothing to link to.
        return _reach_unknown()
    targets = sorted(_visible_owner_repos(session, caller.principal).items())[:_REACH_MAX_OWNERS]
    # Release the transaction before the network hop (`token_for` may have committed; the rule holds).
    session.rollback()
    if app.kind == "github_app":
        return _installation_reach(token, app.slug, [owner for owner, _repo in targets])
    answers: list[Optional[str]] = []
    if targets:
        with ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="git-reach") as pool:
            answers = list(pool.map(lambda target: _probe(token, target[1]), targets))
        if all(answer == _UNREADABLE for answer in answers):
            # Unreadable is not "reaches nothing"; an outage must not tell every user their access is gone.
            return _reach_unknown()
    client_id = app.client_id.strip()
    return GitReachOut(
        state="ok",
        # None for a blank client id: a link to GitHub's bare connections list would name no app.
        approve_url=(
            f"https://github.com/settings/connections/applications/{quote(client_id, safe='')}"
            if client_id else None
        ),
        owners=[
            GitReachOwner(owner=owner, reach=answer)
            for (owner, _repo), answer in zip(targets, answers, strict=False)
            if answer in ("ok", "approval_required", "sso_required")
        ],
    )


def _installation_reach(token: str, slug: str, owners: list[str]) -> GitReachOut:
    """`/reach` for a GitHub App token: each owner is `ok` when the App is installed on it and visible to this
    person, else `install_required`; `unknown` without a usable slug (nothing to link to) or when
    `/user/installations` is unreadable. The answer is about THIS person's token: an installation on an account
    they cannot see reads as missing, and the install page then lets them request it."""
    url = install_url(slug)
    if url is None:
        return _reach_unknown()
    installed = _installation_accounts(token, timeout=_REACH_TIMEOUT_SECONDS)
    if installed is None:
        return _reach_unknown()
    return GitReachOut(
        state="ok",
        install_url=url,
        installed_count=installed.count,
        owners=[
            GitReachOwner(owner=owner, reach="ok" if owner in installed.accounts else "install_required")
            for owner in owners
        ],
    )
