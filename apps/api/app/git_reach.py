"""Whether a git login can WRITE to one repository, asked of the forge before the login is served, and the
starter's other credentials for that forge root tried when it positively cannot.

`git_access` decides WHOSE credential a piece of work uses; this module only reorders among that one person's
own credentials. The selected connection is served unless GitHub plainly says it cannot write; then the
person's other credentials of the same forge and root (their own, as `authz.git_identity_select` allows, not
revoked; at most `_MAX_OTHERS`, read with their stored token, never renewed here) are asked in turn and the
first that can write is served. When none can, the selected one is still served — reads and clones keep
working — with `problem`, the sentence naming what is missing and where to fix it, so a refused write says
exactly that (`routes/agent/task_secrets.py::GitTokenOut.write_problem`). A selection `git_access` refuses
(none, revoked, deleted) is refused as before: only a plain `no` moves work, never a refusal or `unknown`.

GitHub only; every other forge answers `unknown`. A GitHub App user token writes only where the App is
installed and the installation includes the repository with "Contents: write" (`/user/installations`, then
that installation's repositories when it is `selected`); any other GitHub token reads `permissions.push` off
`GET /repos/{owner}/{repo}`. `no` only on GitHub's plain answer: an outage, a rate limit or a body we cannot
read is `unknown`, and `unknown` never moves work off the selected credential. Answers are cached per
(credential, token, repository) — `_TTL_SECONDS`, `unknown` only `_UNKNOWN_TTL_SECONDS` — so a runner's retries
do not re-ask GitHub and an outage costs one wait, not one per git call; an installation added on github.com
is seen once the entry lapses, a reconnect at once (a new token). The whole pick stops asking past
`_BUDGET_SECONDS`. Known gaps, both towards serving: an installation on `all` repositories is not checked
against the person's own role, and `permissions.push` is the person's role, not a fine-grained token's scope.
Nothing here logs a token.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Literal, NamedTuple, Optional
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app import git_access, github_api, github_oauth
from app.authz.principal import principal_for_user
from app.authz.visibility import git_identity_select
from app.forges import RepoRef
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.tasks import Task

log = logging.getLogger("app.git_reach")

Verdict = Literal["yes", "no", "unknown"]

# One GitHub request; a task's token fetch waits on at most a few of these.
_TIMEOUT_SECONDS = 5.0
# How long one (credential, token, repository) answer stands; `unknown` (an outage, a rate limit) briefly.
_TTL_SECONDS = 300.0
_UNKNOWN_TTL_SECONDS = 60.0
# The other credentials asked at most, and the wall clock one pick may spend asking GitHub.
_MAX_OTHERS = 5
_BUDGET_SECONDS = 10.0
# Pages of an installation's repositories read (100 each) before the answer is `unknown`.
_MAX_PAGES = 10
_PAGE_SIZE = 100


class Reach(NamedTuple):
    """One credential's write reach on one repository. `problem` and `fix_url` are set only for `no`."""

    verdict: Verdict
    problem: str = ""
    fix_url: Optional[str] = None


class Pick(NamedTuple):
    """The credential to serve and, when no credential of the person's can write, why (`problem`, `fix_url`).
    `fallback` is True when the person's selected connection could not write and another of theirs is served."""

    credential: Credential
    problem: str = ""
    fix_url: Optional[str] = None
    fallback: bool = False


_cache: dict[tuple[UUID, str, str, str], tuple[float, Reach]] = {}
_cache_lock = threading.Lock()


def forget() -> None:
    """Drop every cached answer (tests)."""
    with _cache_lock:
        _cache.clear()


def write_reach(credential: Credential, token: str, repo: RepoRef) -> Reach:
    """`credential`'s write reach on `repo`, cached (see the module header). `unknown` off github.com."""
    if not _on_github(repo):
        return Reach("unknown")
    # A digest, never the token: a reconnect (a new token) is a new key, and the cache holds nothing secret.
    key = (credential.id, hashlib.sha256(token.encode()).hexdigest()[:16], repo.root, repo.path.lower())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit[0] < (_UNKNOWN_TTL_SECONDS if hit[1].verdict == "unknown" else _TTL_SECONDS):
        return hit[1]
    reach = _github_reach(credential.name, token, repo.path)
    with _cache_lock:
        for stale in [k for k, (at, _r) in _cache.items() if now - at >= _TTL_SECONDS]:
            del _cache[stale]
        _cache[key] = (now, reach)
    return reach


def _on_github(repo: RepoRef) -> bool:
    return repo.forge == "github" and repo.root == "github.com"


def writer_for_task(session: Session, task: Task, repo: RepoRef) -> Pick:
    """`writer_for_user` for the person `git_access.git_person_for_task` names; with nobody, the refusal
    `git_access.resolve_for_task` raises."""
    person_id = git_access.git_person_for_task(session, task)
    if person_id is None:
        return Pick(git_access.resolve_for_task(session, task, repo))
    return writer_for_user(session, person_id, repo)


def writer_for_user(session: Session, user_id: UUID, repo: RepoRef) -> Pick:
    """The credential of `user_id`'s to serve for `repo` (see the module header). Raises what
    `git_access.resolve_for_user` raises when the person has no usable selection and no other credential of
    theirs can write."""
    selected = git_access.resolve_for_user(session, user_id, repo)
    if not _on_github(repo):
        return Pick(selected)
    deadline = time.monotonic() + _BUDGET_SECONDS
    try:
        # Renewed as serving it would; a renewal that fails is `git_login_for`'s to report, never a reason to move.
        reach = write_reach(selected, git_access.token_for(session, selected), repo)
    except git_access.GitAccessError:
        return Pick(selected)
    if reach.verdict != "no":
        return Pick(selected)
    tried: list[str] = []
    for other in _others(session, user_id, repo, exclude=selected.id):
        token = git_access.readable_token(other)
        if token is None or time.monotonic() >= deadline:
            continue
        if write_reach(other, token, repo).verdict == "yes":
            log.info("git reach: user=%s repo=%s served credential=%s (selected credential=%s cannot write)",
                     user_id, repo.path, other.id, selected.id)
            return Pick(other, fallback=True)
        tried.append(other.name)
    problem = reach.problem
    if tried:
        problem += f" Your other {repo.root} credentials cannot write there either: {', '.join(tried)}."
    return Pick(selected, problem=problem, fix_url=reach.fix_url)


def _others(session: Session, user_id: UUID, repo: RepoRef, *, exclude: UUID) -> list[Credential]:
    """The person's own other credentials that authenticate `repo`'s root, newest first, at most `_MAX_OTHERS`:
    the ones they may push as (`authz.git_identity_select`, the selection's own rule) AND own (`owner_id`), of the
    forge's type, not revoked."""
    user = session.get(AdminUser, user_id)
    if user is None:
        return []
    rows = session.scalars(
        git_identity_select(principal_for_user(session, user))
        .where(Credential.owner_id == user_id, Credential.type == repo.forge, Credential.id != exclude)
        .order_by(Credential.updated_at.desc())
    ).all()
    out: list[Credential] = []
    for credential in rows:
        if git_access.credential_root(credential) != repo.root:
            continue
        if (credential.config or {}).get("grant_dead_at"):
            continue
        out.append(credential)
    return out[:_MAX_OTHERS]


def _github_reach(name: str, token: str, owner_repo: str) -> Reach:
    try:
        if github_oauth.is_app_issued(token):
            return _app_reach(token, owner_repo)
        return _token_reach(name, token, owner_repo)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("git reach: GitHub unreadable (%s)", type(exc).__name__)
        return Reach("unknown")


def _get(token: str, path: str, params: Optional[dict[str, Any]] = None) -> tuple[int, Any]:
    response = httpx.get(f"https://api.github.com/{path}", params=params, headers=github_api.headers(token),
                         timeout=_TIMEOUT_SECONDS, follow_redirects=False)
    if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
        raise ValueError("rate limited")
    return response.status_code, response.json() if response.content else None


def _token_reach(name: str, token: str, owner_repo: str) -> Reach:
    """An OAuth App or pasted token: GitHub's own `permissions.push` for this token's user."""
    owner, _, repo = owner_repo.partition("/")
    status, body = _get(token, f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
    if status == 404:
        return Reach("no", f"Credential '{name}' cannot see {owner_repo} on GitHub. Grant its account access "
                           f"to {owner_repo}, or use a token that has it.")
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("permissions"), dict):
        return Reach("unknown")
    if body["permissions"].get("push") is True:
        return Reach("yes")
    if body["permissions"].get("push") is False:
        return Reach("no", f"Credential '{name}' has read-only access to {owner_repo} on GitHub. Give its "
                           f"account write access to {owner_repo}, or use a token that has it.")
    return Reach("unknown")


def _app_reach(token: str, owner_repo: str) -> Reach:
    """A GitHub App user token: the App installed on the owner, the repository in that installation, and
    "Contents: write" on it."""
    owner = owner_repo.split("/", 1)[0]
    installation = _installation_on(token, owner)
    if installation is None:
        return Reach("no", f"The GitHub App is not installed on {owner}, so it cannot write to {owner_repo}. "
                           f"Install it on {owner} with access to {owner_repo}.", _install_url())
    settings_url = _github_url(installation.get("html_url"))
    permissions = installation.get("permissions")
    if isinstance(permissions, dict) and permissions.get("contents") != "write":
        return Reach("no", f"The GitHub App's installation on {owner} lacks Contents: write. Approve the App's "
                           "requested permissions in the installation's settings.", settings_url)
    if installation.get("repository_selection") == "all":
        return Reach("yes")
    found = _installation_has(token, installation.get("id"), owner_repo)
    if found is None:
        return Reach("unknown")
    if found:
        return Reach("yes")
    return Reach("no", f"The GitHub App's installation on {owner} does not include {owner_repo}. Add "
                       f"{owner_repo} to the installation's repository access.", settings_url)


def _installation_on(token: str, owner: str) -> Optional[dict[str, Any]]:
    """The installation on `owner` visible to this token, or None. Raises `ValueError` when unreadable."""
    for page in range(1, _MAX_PAGES + 1):
        status, body = _get(token, "user/installations", {"per_page": _PAGE_SIZE, "page": page})
        entries = body.get("installations") if status == 200 and isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise ValueError("installations unreadable")
        for entry in entries:
            account = entry.get("account") if isinstance(entry, dict) else None
            if isinstance(account, dict) and str(account.get("login", "")).lower() == owner.lower():
                return entry
        if len(entries) < _PAGE_SIZE:
            return None
    raise ValueError("too many installations")


def _installation_has(token: str, installation_id: Any, owner_repo: str) -> Optional[bool]:
    """Whether installation `installation_id` includes `owner_repo`; None when that cannot be read."""
    if not isinstance(installation_id, int):
        return None
    for page in range(1, _MAX_PAGES + 1):
        status, body = _get(token, f"user/installations/{installation_id}/repositories",
                            {"per_page": _PAGE_SIZE, "page": page})
        repos = body.get("repositories") if status == 200 and isinstance(body, dict) else None
        if not isinstance(repos, list):
            return None
        if any(isinstance(r, dict) and str(r.get("full_name", "")).lower() == owner_repo.lower() for r in repos):
            return True
        if len(repos) < _PAGE_SIZE:
            return False
    return None


def _github_url(value: object) -> Optional[str]:
    """`value` when it is a page on github.com, else None: it becomes a link a person clicks."""
    return value if isinstance(value, str) and value.startswith("https://github.com/") else None


def _install_url() -> Optional[str]:
    """The designated GitHub App's install page, or None when none is designated."""
    from app.routes.git_connect import install_url  # the one home of that URL; a route module, so imported late

    try:
        app = git_access.app_config(kind="github_app", open_client_secret=False)
    except git_access.GitAccessError:
        return None
    return install_url(app.slug)
