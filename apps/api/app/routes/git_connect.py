"""Connect GitHub — the browser round trip that turns a Jaah user into their own git credential, the GitHub
App's install hop, and the Git tab's per-org reach (`/reach`).

The browser half of per-person git access (the work's starter's own credential), through whichever GitHub
app `git_access.app_config` serves — the GitHub App by default, or the classic OAuth App: `app/github_oauth.py`
owns the wire, `app/git_access.py` the policy, and this the two HTTP doors plus the state between them — one
short-lived signed cookie (`app/oauth_flow_cookie.py`) carrying state, PKCE verifier and user id, signed with
`SESSION_SECRET` under its own salt. It is `SameSite=Lax` because the callback is a cross-site navigation from
github.com; `/start` re-mints the session cookie so older `Strict` jars do not 401 there.

Storing the grant is `git_connections.connect_github`'s, shared with a GitHub sign-in. A token of the other
kind than the designated app claims (`git_access.minted_by`) is refused and revoked (`wrong_app_kind`). After a
GitHub App connect, a person whose account reaches no installation is carried to the App's install page
(`needs_installation`), where GitHub lets a non-owner request an owner's approval; `/setup` brings them back.
Nothing logs or echoes a secret; browser-visible failures are slugs from the closed `_ERRORS`.
`app/user_activity.py` excludes `/api/git-connect` from audit capture (the callback's query carries a
live `code`), and `apps/web/nginx.conf.template` keeps it out of nginx logs. Residual (a foreign
proxy) is tolerable: the code is single-use, short-lived and PKCE-bound.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, NamedTuple, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forges, git_access, git_connections, github_api, github_oauth, login_funnel, oauth_flow_cookie
from app.auth import require_user
from app.oauth_flow_cookie import FlowCookie
from app.authz.catalog import PROJECTS_VIEW_KEY, Tenancy
from app.authz.declarations import always, feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_project_select
from app.config import get_settings
from app.db import get_session
from app.models.identity import AdminUser
from app.models.projects import Project
from app.tenancy.ids import is_operator_plane

log = logging.getLogger("app.git_connect")

router = APIRouter(tags=["git-connect"])

# The root every GitHub OAuth token authenticates (`git_connections.root_ref`).
_GITHUB_ROOT = forges.RepoRef(forge="github", host="github.com", root="github.com", path="")

# The cookie mechanism lives in `app/oauth_flow_cookie.py`, shared with the credential Connect flow;
# only this flow's parameters are here. The salt differs from the session cookie's, so neither can be
# replayed as the other. The path is scoped to this flow's routes.
FLOW_COOKIE_PATH = "/api/git-connect"
# See `git_connect_setup`.
SETUP_MISTYPED_PATH = "/api/git-connect/callback/api/git-connect/setup"

# The name derives from the session cookie's, inheriting `SESSION_COOKIE_NAME` (needed when two local
# stacks share a host).
_FLOW = FlowCookie(
    suffix="_git_connect",
    path=FLOW_COOKIE_PATH,
    salt="git-connect-flow",
    members=("state", "verifier", "user_id", "app"),
    # The app kind `/start` authorized against, so the callback exchanges with the same one even if the
    # operator switches apps mid-attempt. Optional: a cookie minted before it existed re-resolves.
    optional=("app",),
)

# Re-exported for tests and routes.
FLOW_COOKIE_NAME = _FLOW.name

# The console pages a connect may return to: Profile's Git tab (the default) and the popup the create wizard
# opens for a hand-off owner, which tells its opener and closes.
# A closed allow-list, never a free-form target: anything else returns to Profile, so `return_to` cannot be
# an open redirect.
RETURN_PATHS = frozenset({"/profile", "/git-connect/done"})
DEFAULT_RETURN = "/profile"

# Where `/start` was asked to come back to. Its own cookie, not a flow-cookie member: the flow cookie is
# cleared at `/callback`, and the App's install hop lands on `/setup` afterwards, with no `state` GitHub
# echoes. Same path scope, signing and ten-minute life as the flow cookie; read back through `_return_to`.
_RETURN = FlowCookie(
    suffix="_git_connect_return",
    path=FLOW_COOKIE_PATH,
    salt="git-connect-return",
    members=("return_to",),
)
RETURN_COOKIE_NAME = _RETURN.name

# The post-exchange login read's budget; the user is waiting on a redirect, and the answer only names the row.
_LOGIN_TIMEOUT_SECONDS = 15.0


# The closed vocabulary of everything the browser is told: no GitHub error text reaches our redirect,
# and each slug names a distinct fix.
_ERRORS = oauth_flow_cookie.closed_errors({
    "not_configured": "no GitHub app is designated in Settings",
    "expired": "the connect attempt was not finished within ten minutes, or its cookie is gone",
    "state_mismatch": "the callback's state did not match the one this browser started with",
    "wrong_user": "the connect attempt belongs to a different signed-in user",
    "denied": "the authorization was declined at GitHub",
    "no_code": "GitHub returned no authorization code",
    "exchange_failed": "GitHub refused to exchange the authorization code",
    "wrong_app_kind": (
        "the credential designated as the GitHub App holds an OAuth App, or the reverse — an operator must fix "
        "the setting"
    ),
    "install_requested": (
        "the GitHub App installation still needs an organization owner's approval, so this connection "
        "cannot reach that organization's repositories yet"
    ),
})


class GitConnectStartIn(BaseModel):
    """`/start`'s optional body: the console page to come back to (not in `RETURN_PATHS` → Profile), and the app
    to authorize against (`git_access.APP_KNOBS`; anything else → the one Connect uses now). The create wizard
    names `oauth_app` for an owner the GitHub App may not create in: that token creates with the person's own
    rights. Any signed-in person may name either designated app: both are the operator's, and the token
    is only ever their own account's, approved by them at GitHub, so `git_connect_app` stays a default, not a cap."""

    return_to: Optional[str] = None
    app: Optional[str] = None


class GitConnectStartOut(BaseModel):
    """What `/start` hands the browser: the URL to navigate to, and nothing else (state, challenge and
    user id are already in the URL or the cookie)."""

    authorize_url: str


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


def console_url(path: str) -> str:
    """`path` on the console's own origin, from `frontend_url` and never the request: a callback URL is a binding
    on the exchange, not just a destination, so it must be byte-identical on `/start` and the exchange, and
    `/api` is same-origin with the frontend everywhere (vite proxy, nginx). Every connect flow's redirects
    are built here (`routes/forge_connect.py` too)."""
    return f"{get_settings().frontend_url.rstrip('/')}{path}"


def _redirect_uri() -> str:
    """The callback URL registered at the GitHub app, absolute (`console_url` says why not the request)."""
    return console_url("/api/git-connect/callback")


def _allowed_return(return_to: Optional[str]) -> str:
    """`return_to` when it is one of `RETURN_PATHS`, else `DEFAULT_RETURN`."""
    return return_to if return_to in RETURN_PATHS else DEFAULT_RETURN


def _return_to(request: Request) -> str:
    """The page this browser's `/start` asked to come back to; Profile when there is no valid cookie."""
    stored = _RETURN.read(request)
    return _allowed_return(stored["return_to"] if stored else None)


def finish_redirect(
    page: str, errors: dict[str, str], *, error: Optional[str] = None, **tags: str
) -> RedirectResponse:
    """A 303 to the console `page` saying how a connect ended: `git_connect=connected`, or `git_connect=error`
    with `reason` one slug of the flow's closed `errors` (checked, never a message), then `tags` (the other
    forges' flow names its forge).

    303 so the follow is a plain GET and a reload cannot re-submit the spent code."""
    if error is not None and error not in errors:
        raise ValueError(f"{error!r} is not a member of the flow's closed error vocabulary")
    if {"git_connect", "reason"} & tags.keys():
        raise ValueError("a tag may not shadow the checked keys")
    query = {"git_connect": "error", "reason": error} if error else {"git_connect": "connected"}
    return RedirectResponse(f"{console_url(page)}?{urlencode({**query, **tags})}", status_code=303)


def _return_redirect(return_to: str, *, error: Optional[str] = None) -> RedirectResponse:
    """`finish_redirect` to `return_to`, re-checked against `RETURN_PATHS`."""
    return finish_redirect(_allowed_return(return_to), _ERRORS, error=error)


_TENANT_UNCONFIGURED_DETAIL = "GitHub connect is not set up. Ask an operator."


def _app_config(*, operator: bool, kind: Optional[str] = None) -> git_access.AppConfig:
    """The GitHub app Connect uses now (`git_access.app_config`), or a 503 whose detail depends on who is
    asking.

    One operator-tier app of each kind for the whole deployment. 503, not 400: the
    fix is an operator setting. The operator plane (`is_operator_plane`, not `is_tenant_org` or
    `is_superadmin` alone) gets `GitAccessError`'s setting-naming text; everyone else gets a sentence for a
    person, since they cannot see or change that setting."""
    try:
        app = git_access.app_config(kind=git_access.known_app_kind(kind))
    except git_access.GitAccessError as exc:
        detail = str(exc) if operator else _TENANT_UNCONFIGURED_DETAIL
        raise HTTPException(status_code=503, detail=detail) from exc
    return app


@router.post("/api/git-connect/start", response_model=GitConnectStartOut)
@feature("credentials", "create", own_credential=True)
def start_git_connect(
    request: Request,
    response: Response,
    body: Optional[GitConnectStartIn] = Body(default=None),
    user: AdminUser = Depends(require_user),
) -> GitConnectStartOut:
    """Begin a connect attempt: mint the state, stash it and the page to return to in the browser, return
    where to go.

    `own_credential=True` over `credentials:create` makes this self-service
    (`authz.Declaration.own_credential`); an undeclared route would be denied. No route here takes a
    user id: the account is always the calling session's. No DB session and no write: an abandoned
    attempt leaves only a cookie."""
    app = _app_config(operator=is_operator_plane(user.org_id, user.is_superadmin), kind=body.app if body else None)
    pkce = github_oauth.new_pkce_pair()
    state = github_oauth.new_state()
    url = github_oauth.authorize_url(
        client_id=app.client_id,
        redirect_uri=_redirect_uri(),
        state=state,
        challenge=pkce.challenge,
        # No `login` hint: guessing a handle could pre-fill the wrong account. The account picker instead:
        # without it GitHub silently re-approves whichever account is signed in, with no chance to switch.
        prompt="select_account",
    )
    _FLOW.set(response, {"state": state, "verifier": pkce.verifier, "user_id": user.id, "app": app.kind})
    # Always written, the default too: an earlier attempt's page must not outlive this one.
    _RETURN.set(response, {"return_to": _allowed_return(body.return_to if body else None)})
    # Re-mint the session cookie (same value) to upgrade an older `SameSite=Strict` jar entry to
    # `lax`; otherwise the cross-site callback 401s. A no-op for an already-`lax` cookie.
    login_funnel.reissue_session(request, response, user)
    log.info("git-connect: user=%s started an attempt", user.id)
    return GitConnectStartOut(authorize_url=url)


@router.get("/api/git-connect/callback")
@feature("credentials", "create", own_credential=True)
def git_connect_callback(
    request: Request,
    # Every query param is an unconstrained defaulted `str`: a pydantic 422 would echo the rejected
    # input, an authorization code (same rule as `routes/credentials/`).
    code: str = Query(default=""),
    state: str = Query(default=""),
    # GitHub sends `error=access_denied` when the user declines — an ordinary outcome.
    error: str = Query(default=""),
    user: AdminUser = Depends(require_user),
    # For the reconnect lookup (`git_connections.connect_github`, the caller's own rows).
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """GitHub's redirect target: verify, exchange, store, point the profile at it, go back to the page `/start`
    named (`_return_to`).

    Before the code is spent (`oauth_flow_cookie.verified_attempt`): a valid flow cookie exists; its state
    equals the query's, in constant time (login CSRF); and its user id equals the signed-in user, so a
    callback can never attach a GitHub account to a different user's profile (shared browser, re-login,
    planted cookie). A refusal leaves the code unexchanged.

    Failures redirect to that page with a closed `_ERRORS` slug rather than raising, so the person gets a
    page that explains and offers the button again, never GitHub's own text."""
    # The shared checks and their order live in `oauth_flow_cookie.verified_attempt`; its slugs are
    # members of `_ERRORS`.
    return_to = _return_to(request)
    flow, refusal = oauth_flow_cookie.verified_attempt(
        _FLOW, request,
        callback_state=state, user_id=user.id,
        log=log, flow_name="git-connect",
    )
    if refusal is not None:
        return _finish(_return_redirect(return_to, error=refusal))
    assert flow is not None  # narrowing: `verified_attempt` returns exactly one of the two
    if error:
        # Here, not in the shared helper: a decline is not about trust. `error` only chooses our slug and
        # is never echoed.
        log.info("git-connect: user=%s did not complete authorization at GitHub", user.id)
        return _finish(_return_redirect(return_to, error="denied"))
    if not code:
        # After the decline branch: a decline legitimately carries no code.
        return _finish(_return_redirect(return_to, error="no_code"))

    # The kind `/start` authorized against: a code is bound to the client that issued it.
    app = _app_config(operator=is_operator_plane(user.org_id, user.is_superadmin), kind=flow["app"])
    try:
        grant = github_oauth.exchange_code(
            client_id=app.client_id,
            client_secret=app.client_secret,
            code=code,
            # Byte-identical to `/start`'s, or GitHub answers `redirect_uri_mismatch`.
            redirect_uri=_redirect_uri(),
            verifier=flow["verifier"],
        )
    except github_oauth.GitHubOAuthError as exc:
        # `exc.code` is safe to log; `str(exc)` may carry GitHub's text and never reaches the browser.
        log.warning("git-connect: user=%s exchange refused code=%s", user.id, exc.code)
        return _finish(_return_redirect(return_to, error="exchange_failed"))
    if not git_access.minted_by(app, grant.access_token):
        # The row designated as one kind holds the other kind's client pair: never stored.
        log.warning("git-connect: user=%s was issued a token of the wrong kind for the designated %s; token "
                    "revoked, nothing stored", user.id, app.kind)
        github_oauth.revoke_user_token(
            client_id=app.client_id, client_secret=app.client_secret, access_token=grant.access_token
        )
        return _finish(_return_redirect(return_to, error="wrong_app_kind"))

    login = _github_login(grant.access_token)
    connected = git_connections.connect_github(
        session, user, principal, grant, login=login, via="Connect GitHub"
    )
    credential_id = connected.credential.id
    session.commit()
    git_connections.revoke_replaced(connected, grant)
    log.info(
        "git-connect: user=%s connected credential id=%s via=%s expiring=%s",
        user.id, credential_id, app.kind, grant.expires_at is not None,
    )
    if app.kind != "github_app":
        return _finish(_return_redirect(return_to))
    # The installation carry-forward. The connect is already committed, so this may only add a redirect.
    # The blanket `except` covers it not returning at all: `_managed_owner_accounts` opens a fresh
    # transaction, and e.g. pool exhaustion must not 500 a successful connect or skip `_finish`.
    try:
        carry_forward = needs_installation(session, app.slug, login, grant.access_token)
    except Exception:  # noqa: BLE001 — the connect is committed; nothing here may be allowed to fail it
        log.warning("git-connect: user=%s installation carry-forward failed — sending them to "
                    "Profile with the connect intact", user.id, exc_info=True)
        carry_forward = False
    if carry_forward:
        # The return cookie stays: `/setup` reads it when GitHub lands the browser back.
        return _finish(_install_redirect(app.slug), keep_return=True)
    return _finish(_return_redirect(return_to))


def _finish(response: RedirectResponse, *, keep_return: bool = False) -> RedirectResponse:
    """Clear the attempt cookie on the way out, on every exit including the failures, and the return cookie
    too unless `keep_return` (the install hop, whose `/setup` still reads it).

    One wrapper so a later-added exit cannot forget, leaving a verifier and user id in the browser."""
    _FLOW.clear(response)
    if not keep_return:
        _RETURN.clear(response)
    return response


def _github_login(access_token: str) -> Optional[str]:
    """The connected account's login, or None — and None is not a failure of the connect.

    The code is already spent, so every branch degrades to None rather than stranding a live grant. Only
    an exception class or HTTP status is logged, never the token."""
    try:
        response = httpx.get(
            "https://api.github.com/user",
            headers=github_api.headers(access_token),
            timeout=_LOGIN_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        # The class only: `str(exc)` renders the request URL.
        log.warning("git-connect: could not read the connected login (%s)", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("git-connect: /user answered HTTP %s", response.status_code)
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return github_oauth.clean_login(payload.get("login"))


# ── The GitHub App's installation carry-forward (see this module's header) ──────────────────────────
#
# GitHub's "Request user authorization (OAuth) during installation" must never be enabled: its redirect
# carries only `client_id`, bypassing PKCE and CSRF. So `/callback` decides whether to send the browser
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
        # `_github_login` already warned; this only records the consequence.
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
    access_token: str, *, timeout: float = _LOGIN_TIMEOUT_SECONDS,
) -> Optional[Installations]:
    """What a GitHub App user token can see, or `None` when the answer could not be read at all.

    Suspended installations count as absent (no repository access). `per_page=100` (GitHub's maximum),
    since an account falling off page 1 would re-prompt on every connect. `/user/installations` lists the
    installations the user has explicit permission to access, which is the question asked."""
    try:
        response = httpx.get(
            "https://api.github.com/user/installations",
            params={"per_page": 100},
            headers=github_api.headers(access_token),
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        log.warning("git-connect: could not read installations (%s)", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("git-connect: /user/installations answered HTTP %s", response.status_code)
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    installations = payload.get("installations") if isinstance(payload, dict) else None
    if not isinstance(installations, list):
        return None
    logins: set[str] = set()
    active = 0
    for entry in installations:
        if not isinstance(entry, dict):
            continue
        # A truthy `suspended_at` means suspended, i.e. no reach.
        if entry.get("suspended_at"):
            continue
        active += 1
        account = entry.get("account")
        login = account.get("login") if isinstance(account, dict) else None
        if isinstance(login, str) and login:
            logins.add(login.lower())
    # Not GitHub's `total_count`: it includes suspended installations.
    return Installations(accounts=logins, count=active)


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


def _install_redirect(slug: str) -> RedirectResponse:
    """303 to the designated App's own installation-picker screen (303 as in `_return_redirect`).

    The slug is already validated by `needs_installation`; a None here is a programming error, so it
    raises."""
    url = install_url(slug)
    if url is None:  # pragma: no cover - `needs_installation` refuses the slug before this is reached
        raise ValueError("_install_redirect was handed a slug _APP_SLUG refuses")
    return RedirectResponse(url, status_code=303)


@router.get("/api/git-connect/setup")
# The App's Setup URL saved as the Callback URL with this path appended. Prod's was, and every install
# then 404'd. Serving it too means that one paste error cannot strand a user.
@router.get(SETUP_MISTYPED_PATH)
@always(Tenancy.BOOTSTRAP)
def git_connect_setup(
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

    No flow cookie (it was cleared at `/callback`; nothing to verify); the return cookie `/start` set names
    the page to land on, allow-listed (`_return_to`), and is retired here. `@always`: the response carries
    nothing about anyone."""
    return_to = _return_to(request)
    response = _return_redirect(return_to, error="install_requested" if setup_action == "request" else None)
    _RETURN.clear(response)
    return response


# ── The Git tab's org reach (see `GitReachOut`) ────────────────────────────────────────────────────

# Shorter than `_LOGIN_TIMEOUT_SECONDS`: this sync handler holds a threadpool worker on a repeatable GET,
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
    could not be reached. Only a status or an exception class is logged."""
    owner, _, repo = owner_repo.partition("/")
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}",
            headers=github_api.headers(token),
            timeout=_REACH_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        log.warning("git-connect: reach probe failed (%s)", type(exc).__name__)
        return _UNREADABLE
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return _classify(response.status_code, payload)


@router.get("/api/git-connect/reach", response_model=GitReachOut)
@feature("credentials", "view", own_credential=True)
def git_connect_reach(
    user: AdminUser = Depends(require_user),
    # For the owner list (project data, grant-scoped), not the credential; see `_visible_owner_repos`.
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GitReachOut:
    """Which GitHub orgs behind the caller's projects their connection reaches, and which an org's OAuth
    App access restrictions, SAML SSO or a missing GitHub App installation withholds, for the Git tab (see
    `GitReachOut`). The connection's own token says which app made it (`git_access.token_app_kind`), so a
    connection from the app not chosen now is still described truthfully.

    DB reads first, then the transaction is released before the GitHub hop so no connection idles in
    transaction (`app/db.py`). Takes no user id; `own_credential=True` over `credentials:view` makes it
    self-service, and `resolve_for_user` re-authorizes the selection. Nothing is written or cached: an org's
    policy and installations change on github.com with no event we see."""
    token = _connected_token(session, user)
    if token is None:
        return _reach_unknown()
    try:
        app = git_access.app_config(kind=git_access.token_app_kind(token), open_client_secret=False)
    except git_access.GitAccessError:
        # The app that made this connection is no longer designated: nothing to link to.
        return _reach_unknown()
    targets = sorted(_visible_owner_repos(session, principal).items())[:_REACH_MAX_OWNERS]
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
            for (owner, _repo), answer in zip(targets, answers)
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
