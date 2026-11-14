"""One-repo git logins for the runners of a Jaah-made repository, minted by the console's GitHub App.

A Jaah-made repository (`app/jaah_repos.py`) lives in Jaah's own GitHub org (`easy_mode_github_org`). No person's
token and no admin credential may reach the node its runners work on, so its git login is an installation access
token of the console's GitHub App (the one Connect GitHub and sign-in use, `git_access.APP_KNOBS["github_app"]`),
installed on that org and restricted to exactly the one repository and the permissions its state allows
(`jaah_repos.permissions_for`: write while `active`, read-only while it moves). The App's private key, an optional
field of that credential, stays on the control plane: only the minted token (an hour's life) is served.

Seam: `routes/agent/task_secrets.py` asks `jaah_repos.is_jaah_repo` before the person resolution of both git-token
routes; everywhere else resolution is unchanged. The App's installation on the org, with the permissions it grants, is
looked up per (credential, org) and cached for `_INSTALLATION_TTL_SECONDS`, so a permission an operator grants the App
later reaches the next mint without a restart; tokens are cached per (credential, installation, repository,
permissions) until `_EXPIRY_MARGIN` before they expire. Never log a token, the JWT or the key.
"""
from __future__ import annotations

import logging
import re
import textwrap
import threading
import time
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

import httpx
import jwt

from app import git_access, github_api, jaah_repos
from app.clock import as_utc
from app.forges import RepoRef
from app.git_access import GitAccessError, GitLogin

log = logging.getLogger("easy-mode-runner-tokens")

#: Where the App is picked, for refusals that name it.
_WHERE = f"'{git_access._APP_KNOB_LABELS['github_app']}' under Settings > Integrations > GitHub"
#: Exactly what a runner of an `active` repo may do with it: push its branch, open its PR, edit the deploy workflow.
PERMISSIONS = jaah_repos.WRITE_PERMISSIONS
#: A cached token is served until this long before GitHub's `expires_at`.
_EXPIRY_MARGIN_SECONDS = 5 * 60
#: How long the installation lookup (its id and granted permissions) is reused before GitHub is asked again.
_INSTALLATION_TTL_SECONDS = 10 * 60
#: GitHub's permission levels, weakest first.
_LEVELS = ("read", "write")
#: The Variables permission: installations report it, GitHub's token-request schema does not list it, so a mint
#: asking for it that GitHub refuses (422) is tried once more without it (`_mint_tolerant`).
_UNLISTED = "actions_variables"
#: A token minted without `_UNLISTED` is served at most this long, so a later fix reaches runners soon.
_NARROWED_LIFE_SECONDS = 15 * 60
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0)


@dataclass(frozen=True)
class _Installation:
    id: str
    #: What the installation grants (`permissions` of `GET /orgs/{org}/installation`), e.g. {"checks": "write"}.
    permissions: dict[str, str]
    fetched_at: float


@dataclass(frozen=True)
class _Cached:
    token: str
    expires_at: float  # epoch seconds

    def __repr__(self) -> str:  # never the token
        return f"_Cached(expires_at={self.expires_at})"


#: Keyed by (credential, App, installation, repository id, permissions): a re-entered credential never serves the old
#: App's token, and a read-only login never a write one.
_CacheKey = tuple[UUID, str, str, int, tuple[tuple[str, str], ...]]
_cache: dict[_CacheKey, _Cached] = {}
#: The App's installation on the org, keyed by (credential, client id, org): one lookup per TTL, not one per mint.
_installations: dict[tuple[UUID, str, str], _Installation] = {}
#: One lock per key, so a slow mint for one repository never holds up another's; `_lock` guards the two dicts.
_key_locks: dict[_CacheKey, threading.Lock] = {}
_lock = threading.Lock()


def login_for(repo: RepoRef, *, repo_id: int, permissions: Optional[dict[str, str]] = None,
              optional: Optional[dict[str, str]] = None, now: Optional[float] = None) -> GitLogin:
    """The git login for `repo` (`jaah_repos.is_jaah_repo` said yes), whose immutable GitHub id is `repo_id`: a
    cached or freshly minted installation token for exactly that repository id — never a name, which another repo
    may hold later — with `permissions` (default `PERMISSIONS`), plus each of `optional` at the level the
    installation grants it, up to the one asked (`_granted`): never one it lacks, which GitHub would refuse the whole
    mint over. `GitAccessError` names what to fix, never a secret."""
    app = _app()
    clock = time.time() if now is None else now
    installation = _installation(app, clock)
    installation_id = installation.id
    granted = {**_granted(optional or {}, installation.permissions),
               **(permissions if permissions is not None else PERMISSIONS)}
    key = (app.credential_id, app.client_id, installation_id, int(repo_id), tuple(sorted(granted.items())))
    with _lock:
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        hit = _cache.get(key)
        if hit is not None and hit.expires_at - _EXPIRY_MARGIN_SECONDS > clock:
            return _login(hit.token)
        try:
            minted = _mint_tolerant(app, installation_id, repo.path, int(repo_id), clock, granted)
        except _MintRefused as exc:
            if exc.status in (401, 404, 422):
                with _lock:  # a moved or removed installation, or a refused permission set, is looked up again
                    _installations.pop((app.credential_id, app.client_id, jaah_repos.github_org().lower()), None)
            raise GitAccessError(
                f"GitHub refused a git login for {repo.path} (HTTP {exc.status}). Check the GitHub App {_WHERE} "
                f"names is installed on {jaah_repos.github_org()} for all repositories, with Contents, Pull requests "
                "and Workflows write access (Checks and Commit statuses too, for reading a PR's checks)."
            ) from None
        with _lock:
            _cache[key] = minted
    log.info("easy mode runner token minted repo=%s credential=%s", repo.path, app.credential_id)
    return _login(minted.token)


def _mint_tolerant(app: _App, installation_id: str, repo_path: str, repo_id: int, now: float,
                   permissions: dict[str, str]) -> _Cached:
    """`_mint`; a 422 on a set carrying `_UNLISTED` is retried once without it, that token capped at
    `_NARROWED_LIFE_SECONDS`, so a key GitHub may not take on a token never costs a runner its login."""
    try:
        return _mint(app, installation_id, repo_path, repo_id, now, permissions)
    except _MintRefused as exc:
        if exc.status != 422 or _UNLISTED not in permissions:
            raise
    minted = _mint(app, installation_id, repo_path, repo_id, now,
                   {k: v for k, v in permissions.items() if k != _UNLISTED})
    log.warning("easy mode runner token: GitHub refused %s for repo=%s; minted without it", _UNLISTED, repo_path)
    return _Cached(token=minted.token,
                   expires_at=min(minted.expires_at, now + _NARROWED_LIFE_SECONDS + _EXPIRY_MARGIN_SECONDS))


def app_installation_token(owner_repo: str, repo_id: int, permissions: dict[str, str]) -> Optional[str]:
    """An installation token of the console's GitHub App for the one repository `repo_id` (`owner_repo`, the
    installation found by `GET /repos/{owner}/{repo}/installation`) with exactly `permissions`, or None: no App
    usable, not installed there, or the installation does not grant `permissions` (GitHub's 422). Never cached and
    never logged. The caller proves the tenant may act on `owner_repo` first: the App's key signs for every org that
    installed it (`production_qa/github_hooks.py`)."""
    if owner_repo.partition("/")[0].lower() == jaah_repos.github_org().lower():
        return None  # Jaah's own org is Easy Mode's alone (`login_for`), never reached through a tenant's repository
    try:
        app = _app()
        now = time.time()
        installation_id = _repo_installation_id(app, owner_repo, now)
        if installation_id is None:
            return None
        return _mint(app, installation_id, owner_repo, int(repo_id), now, dict(permissions)).token
    except GitAccessError:
        return None


def _repo_installation_id(app: _App, owner_repo: str, now: float) -> Optional[str]:
    """The App's installation reaching `owner_repo`, or None (not installed there, or GitHub unreachable)."""
    owner, _, repo = owner_repo.partition("/")
    try:
        with _client(app, now) as client:
            resp = client.get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation")
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("github app installation lookup failed for %s (%s)", owner_repo, type(exc).__name__)
        return None
    found = str(data.get("id") or "") if isinstance(data, dict) else ""
    return found or None


def _login(token: str) -> GitLogin:
    # The same username a GitHub token takes everywhere (`git_access._GIT_LOGINS[("github", "")]`).
    return GitLogin(scheme="basic", username="x-access-token", token=token)


@dataclass(frozen=True)
class _App:
    credential_id: UUID
    #: The JWT's `iss`: GitHub takes an App's client id there as well as its numeric App id.
    client_id: str
    private_key: str

    def __repr__(self) -> str:  # never the key
        return f"_App(credential_id={self.credential_id}, client_id={self.client_id!r})"


def _app() -> _App:
    """The console's GitHub App, read on an operator-scoped session of its own (`git_access.app_config`'s shape):
    `GitAccessError` when it is not picked, not the operator's, or holds no private key."""
    # Deferred: `app.db` installs ORM listeners at import time; route modules import this one.
    from app.db import BridgedSession
    from app.tenancy import seam

    with seam.detached(), seam.operator_scope(), BridgedSession() as session:
        try:
            credential = git_access._designated_app(session, "github_app")
        except GitAccessError as exc:
            raise GitAccessError(
                f"Easy Mode runners get their git login from the GitHub App, and it is not usable: {exc}"
            ) from None
        key = git_access._opened_or_git_error(credential, "private_key", "the GitHub App's private key")
        app = _App(
            credential_id=credential.id,
            client_id=str((credential.config or {}).get("client_id") or "").strip(),
            private_key=_pem(key),
        )
    if not app.client_id:
        raise GitAccessError(f"The GitHub App {_WHERE} names has no Client ID. Re-enter it in that credential.")
    if not app.private_key:
        raise GitAccessError(
            f"The GitHub App {_WHERE} names has no private key, so Easy Mode runners have no git login. Generate "
            "one on the App's General page on GitHub and paste it into that credential."
        )
    return app


def setup_problem() -> Optional[str]:
    """Why Jaah-made repositories' runners cannot get a git login (the operator's words, naming what to fix), or
    None when they can: the App is picked and holds its key. Its installation on the org is checked at the first
    mint, whose refusal names it."""
    try:
        _app()
    except GitAccessError as exc:
        return str(exc)
    return None


_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)


def _pem(raw: str) -> str:
    """`raw` as a well-formed PEM: a single-line secret input drops the key file's line breaks, so the body is
    re-wrapped at 64 columns between its BEGIN/END lines. Anything else is returned stripped, for the signer to
    refuse."""
    match = _PEM.search(raw or "")
    if match is None:
        return (raw or "").strip()
    label, body = match.group(1), re.sub(r"\s+", "", match.group(2))
    return f"-----BEGIN {label}-----\n" + "\n".join(textwrap.wrap(body, 64)) + f"\n-----END {label}-----\n"


def _jwt(app: _App, now: float) -> str:
    """The App's own JWT (RS256, GitHub's ten-minute ceiling; `iat` back-dated a minute for clock drift)."""
    issued = int(now) - 60
    try:
        return jwt.encode({"iat": issued, "exp": issued + 9 * 60, "iss": app.client_id}, app.private_key,
                          algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError):
        raise GitAccessError(
            "The GitHub App's private key could not sign a token. Paste the App's .pem private key again in the "
            f"credential {_WHERE} names."
        ) from None


def _client(app: _App, now: float) -> httpx.Client:
    return httpx.Client(base_url=github_api.GITHUB_API_BASE, timeout=_TIMEOUT, follow_redirects=False,
                        headers=github_api.headers(_jwt(app, now)))


def _granted(optional: dict[str, str], installed: dict[str, str]) -> dict[str, str]:
    """Each of `optional` at the lower of the level asked and the level `installed` grants; one not granted is left
    out."""
    out = {}
    for name, asked in optional.items():
        has = installed.get(name)
        if has in _LEVELS and asked in _LEVELS:
            out[name] = _LEVELS[min(_LEVELS.index(has), _LEVELS.index(asked))]
    return out


def _installation(app: _App, now: float) -> _Installation:
    """The App's installation on the Easy Mode org (`GET /orgs/{org}/installation`) and what it grants, cached per
    credential and org for `_INSTALLATION_TTL_SECONDS`. A refresh GitHub cannot answer (unreachable, 5xx) keeps
    serving the stale lookup (only a 401/404 drops it): an outage or rate limit never breaks a login the last lookup still covers."""
    org = jaah_repos.github_org()
    key = (app.credential_id, app.client_id, org.lower())
    with _lock:
        hit = _installations.get(key)
    if hit is not None and now - hit.fetched_at < _INSTALLATION_TTL_SECONDS:
        return hit
    try:
        with _client(app, now) as client:
            resp = client.get(f"/orgs/{quote(org, safe='')}/installation")
    except httpx.HTTPError as exc:
        log.warning("easy mode runner token: GitHub unreachable (%s)", type(exc).__name__)
        if hit is not None:
            return hit
        raise GitAccessError("GitHub could not be reached to mint this runner's git login. Try again.") from None
    if hit is not None and resp.status_code not in (200, 401, 404):
        log.warning("easy mode runner token: GitHub answered %s; keeping the last installation lookup",
                    resp.status_code)
        return hit
    try:
        data = resp.json() if resp.status_code == 200 else None
    except ValueError:
        data = None
    # Pinned to the org's own account: the key also signs for every tenant org that installed the App for Connect, so
    # nothing but the Easy Mode org's installation may ever mint here.
    account = data.get("account") if isinstance(data, dict) else None
    login = str(account.get("login") or "") if isinstance(account, dict) else ""
    found = str(data.get("id") or "") if isinstance(data, dict) and login.lower() == org.lower() else ""
    if not found:
        log.warning("easy mode runner token: GitHub answered %s for the installation on %s", resp.status_code, org)
        raise GitAccessError(
            f"The GitHub App {_WHERE} names is not installed on {org} (HTTP {resp.status_code}). Install it there "
            "for all repositories."
        )
    raw = data.get("permissions") if isinstance(data, dict) else None
    granted = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    installation = _Installation(id=found, permissions=granted, fetched_at=now)
    with _lock:
        _installations[key] = installation
    return installation


class _MintRefused(GitAccessError):
    """GitHub answered a mint with `status` rather than a token."""

    def __init__(self, status: int) -> None:
        super().__init__(f"GitHub refused the installation token (HTTP {status}).")
        self.status = status


def _mint(app: _App, installation_id: str, repo_path: str, repo_id: int, now: float,
          permissions: dict[str, str]) -> _Cached:
    """`POST /app/installations/{id}/access_tokens`, restricted to the repository `repo_id` and `permissions`: the
    one minting home (`login_for`, `app_installation_token`). `_MintRefused` on any answer but 201."""
    body = {"repository_ids": [repo_id], "permissions": permissions}
    try:
        with _client(app, now) as client:
            resp = client.post(f"/app/installations/{installation_id}/access_tokens", json=body)
    except httpx.HTTPError as exc:
        log.warning("github app installation token: GitHub unreachable (%s)", type(exc).__name__)
        raise GitAccessError("GitHub could not be reached to mint this runner's git login. Try again.") from None
    if resp.status_code != 201:
        log.warning("github app installation token: GitHub answered %s for repo=%s", resp.status_code, repo_path)
        raise _MintRefused(resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        data = None
    token = str(data.get("token") or "") if isinstance(data, dict) else ""
    if not token:
        raise GitAccessError(f"GitHub answered with no token for {repo_path}. Try again.")
    return _Cached(token=token, expires_at=_expiry(data.get("expires_at") if isinstance(data, dict) else None, now))


def _expiry(raw: object, now: float) -> float:
    """GitHub's `expires_at` as epoch seconds; an unreadable one counts as the documented hour."""
    try:
        return as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))).timestamp()
    except (TypeError, ValueError):
        return now + 3600


def clear_cache() -> None:
    """Forget every cached token (tests)."""
    with _lock:
        _cache.clear()
        _key_locks.clear()
        _installations.clear()

