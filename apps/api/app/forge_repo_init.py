"""Making and initializing repositories through a person's own git connection, for the project create wizard
(`POST /api/git-repos`, `POST /api/git-repos/initialize`, and the `empty` of `GET /api/git-repos/default-branch`;
`routes/git_connections.py`).

An EMPTY repository is one with no branch at all (zero commits). Initializing it commits `README.md` and
`.gitignore` to `main`, branches `develop` from that commit, and makes `main` the default branch; `develop` is
the integration branch the wizard sends. An initialization cut short (only `main` exists) is finished by the next
call: `develop` made and the default set. A repository with any other branch is never written. A NEW repository
is created private (`create`), then initialized the same way.

Per forge:
  * GitHub: empty = `GET /repos/{o}/{r}/branches` lists none (or the repo answers 409 "Git Repository is
    empty"); the first commits go through the contents API (refs cannot be created on an empty repository) —
    the first onto the repository's own default branch, renamed to main when it is not — then `git/refs` makes
    develop. New: `POST /user/repos` for the person's own login, else `/orgs/{o}/repos`; through a GitHub App
    whose installation on the owner has "Only select repositories", the new repository is then added to it
    (`PUT /user/installations/{id}/repositories/{repo id}`), or the App could neither initialize nor clone it. An
    existing repository the App's token is refused (a public one reads without the installation) is added the same
    way and written again; where it cannot be, the error names the one step on GitHub that would.
  * GitLab (gitlab.com or self-managed): empty = the project's `empty_repo`; one `repository/commits` call
    writes both files to main, then `repository/branches` makes develop. New: `POST /projects` in the owner's
    namespace (`GET /namespaces/{full path}` names its id).
  * Bitbucket: empty = `refs/branches` lists none, read again right before the write (`src` would commit on top
    of a branch a racing push made; main's new commit is then checked to have no parent); one multipart `src`
    POST writes both files to main, then `refs/branches` makes develop at main's hash. New: `POST /repositories/{workspace}/{slug}` (the
    workspace's oldest project holds it, Bitbucket's own default).
  * Azure DevOps: empty = `refs?filter=heads/` lists none; one `pushes` POST creates main with both files, then
    `refs` makes develop. New: `POST {org}/{project}/_apis/git/repositories`; the owner is `org/project`.

Reached only through the connection `forge_repos.plan_read` resolves; no token is logged or returned. Every
failure raises `ForgeWriteError` with a sentence for the person. A self-managed GitLab's host is vetted once per
write and every request dials an address that check returned (`forge_repos._plan_client`), so a rebinding
domain cannot swap in a private address between the check and a POST or PUT.
"""
from __future__ import annotations

import base64
import dataclasses
import logging
import re
import time
import urllib.parse
from typing import Any, Optional

import httpx

from app import forge_repos, forges, github_api, github_oauth

log = logging.getLogger("app.forge_repo_init")

INIT_BRANCH = "main"
INTEGRATION_BRANCH = "develop"

# Several requests in a row: one budget for the whole write.
_BUDGET_SECONDS = 60.0
_ZERO_SHA = "0" * 40
# The characters every forge takes in a repository name (GitHub's set, the narrowest of the four).
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_GITIGNORE = """\
# Dependencies and build output
node_modules/
dist/
build/
.venv/
__pycache__/
*.pyc

# Local environment and secrets
.env
.env.*
!.env.example

# Editors and OS files
.idea/
.vscode/
.DS_Store
"""


# A forge write that did not happen; the message is shown to the person. `status`: the forge's refusal, if any.
ForgeWriteError = forge_repos.ForgeError


class CreatedUnreachable(ForgeWriteError):
    """`create` made `ref`, but could not give the GitHub App's installation access to it: it exists, uninitialized."""

    def __init__(self, message: str, ref: forges.RepoRef) -> None:
        super().__init__(message)
        self.ref = ref


class GitHubAppNotInstalled(ForgeWriteError):
    """The GitHub App has no installation on `owner`: only an install there lets it write (the route links it)."""

    def __init__(self, owner: str, install_url: Optional[str] = None) -> None:
        where = f" ({install_url})" if install_url else ""
        super().__init__(f"Install the GitHub App on {owner}{where}, then try again.")
        self.owner = owner


class _GrantFailed(Exception):
    def __init__(self, url: str, message: str) -> None:
        super().__init__(message)
        self.url = url


# A GitHub App creates repositories only with the "Administration: write" repository permission (GitHub's
# "Permissions required for GitHub Apps", Repository creation); without it GitHub's 403 says only "Resource not
# accessible by integration", which names no fix. The permission is optional: an owner whose installation lacks it is
# listed as a hand-off (`forge_repos.Source.handoff`) and never reaches `create`; this is for one the read missed.
_INTEGRATION_REFUSED = "not accessible by integration"
GITHUB_APP_CANNOT_CREATE = (
    'The GitHub App lacks "Administration: write" here, so it cannot create repositories. Create the repository on '
    "GitHub, then pick it."
)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name not in (".", "..")


def files_for(project_name: str) -> list[tuple[str, str]]:
    """The first commit's files, as (path, text)."""
    title = project_name.strip() or "Project"
    return [("README.md", f"# {title}\n"), (".gitignore", _GITIGNORE)]


def is_empty(plan: forge_repos._Plan, ref: forges.RepoRef) -> Optional[bool]:
    """Whether `ref` has no branch at all; None when it cannot be read (the wizard then says nothing)."""
    segments = forge_repos.path_segments(ref.path)
    if segments is None:
        return None
    deadline = time.monotonic() + forge_repos._REQUEST_TIMEOUT_SECONDS
    try:
        with forge_repos._plan_client(plan) as client:
            return not _branches(client, plan, segments, deadline)
    except (ForgeWriteError, httpx.HTTPError):
        return None
    except Exception:  # noqa: BLE001 - a hint never fails the form
        log.exception("forge-repo-init: %s emptiness read raised", plan.root)
        return None


def initialize(plan: forge_repos._Plan, ref: forges.RepoRef, project_name: str) -> bool:
    """Give an empty `ref` main (README.md, .gitignore) and develop, or finish one whose initialization was cut
    short (main alone); False, writing nothing, when it has any other branch."""
    segments = forge_repos.path_segments(ref.path)
    if segments is None:
        raise ForgeWriteError("This repository URL cannot be written to.")
    deadline = time.monotonic() + _BUDGET_SECONDS
    label = forges.FORGE_LABELS[plan.forge]
    try:
        with forge_repos._plan_client(plan) as client:
            try:
                return _initialize(client, plan, segments, deadline, project_name)
            except ForgeWriteError as exc:
                # A repository the App's installation does not reach (a public one reads without it) is refused
                # before anything is written: given to the installation, then written once more. A refusal after a
                # write, or for any other reason, keeps GitHub's own sentence.
                if not _github_app_refused(plan, exc) or not _github_unwritten(client, plan, segments, deadline):
                    raise
                _github_reach(client, plan, segments, deadline, exc)
                return _initialize(client, plan, segments, deadline, project_name)
    except httpx.HTTPError as exc:
        log.warning("forge-repo-init: %s initialize failed (%s)", plan.root, type(exc).__name__)
        raise ForgeWriteError(f"{label} could not be reached.") from exc


def _initialize(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, project_name: str,
) -> bool:
    branches = _branches(client, plan, segments, deadline)
    if not branches:
        _INIT[plan.forge](client, plan, segments, deadline, files_for(project_name))
    elif branches != [INIT_BRANCH]:
        return False
    _FINISH[plan.forge](client, plan, segments, deadline)
    return True


def create(plan: forge_repos._Plan, owner: str, name: str) -> forges.RepoRef:
    """Create the private, empty repository `owner/name` on `plan`'s root; its ref. `initialize` follows."""
    if not valid_name(name):
        raise ForgeWriteError('A repository name takes letters, digits, ".", "-" and "_" only.')
    parts = forge_repos.path_segments(owner, 1)
    if parts is None or (plan.forge == "azure_devops" and len(parts) != 2):
        raise ForgeWriteError("Pick an owner from the list.")
    deadline = time.monotonic() + _BUDGET_SECONDS
    label = forges.FORGE_LABELS[plan.forge]
    grant_error = None
    try:
        with forge_repos._plan_client(plan) as client:
            url = _CREATE[plan.forge](client, plan, owner, name, deadline)
    except _GrantFailed as exc:
        url, grant_error = exc.url, str(exc)
    except httpx.HTTPError as exc:
        log.warning("forge-repo-init: %s create failed (%s)", plan.root, type(exc).__name__)
        raise ForgeWriteError(f"{label} could not be reached.") from exc
    ref = _parse(url, plan)
    # The forge's answer names the repository the project will clone: only on the root it was asked on.
    if ref.root != plan.root or ref.forge != plan.forge or forge_repos.path_segments(ref.path) is None:
        raise ForgeWriteError(f"{label} answered a repository URL on another host.")
    if grant_error is not None:
        raise CreatedUnreachable(
            f"Created {ref.path}, but could not add it to the GitHub App's installation ({grant_error}). Add it on "
            "GitHub (the App's Configure page, Repository access), then try again.", ref)
    return ref


def _parse(url: str, plan: forge_repos._Plan) -> forges.RepoRef:
    try:
        return forge_repos.parse_url(url, plan)
    except forges.ForgeUrlError as exc:
        raise ForgeWriteError(f"{forges.FORGE_LABELS[plan.forge]} answered a repository URL Jaah cannot use.") from exc


def _refused(status: int, said: Optional[str]) -> str:
    """`Wording.refused`: what the forge said where it may be echoed (`forge_repos.ECHOED_HOSTS`), else a fix."""
    if status == 401:
        return forge_repos._TOKEN_REFUSED
    if status in (403, 404):
        return "Your connection may not do this here" + (
            f": {said}" if said else ". Check its access (and token scopes) in Profile > Git.")
    return f"The forge answered HTTP {status}" + (f": {said}" if said else ".")


# The lookup's wording, less its unreadable-body refusal: a write's answer may have no body (204).
_WORDING = dataclasses.replace(forge_repos._WORDING, refused=_refused, unreadable=None)


def _call(
    client: httpx.Client, method: str, url: str, deadline: float, *, ok: tuple[int, ...] = (200, 201),
    json: Any = None, data: Optional[dict[str, str]] = None, files: Any = None,
    params: Optional[dict[str, Any]] = None,
) -> Any:
    """One request's JSON body (None when it has none), or `ForgeWriteError` naming what the forge said."""
    return forge_repos.request(client, method, url, deadline, _WORDING, ok=ok, json=json, data=data, files=files,
                               params=params)[0]


# --- Per forge ----------------------------------------------------------------------------------------------
# `_repo` (`forge_repos.repo_api`) and `_branches` (at most two names: enough to tell empty, main alone, and anything
# else) read every forge; each then has its own `_init` (the first commit; `_initialize` follows it with `_finish`), `_finish` (develop
# from main, main the default) and `_create`, whose request order is the forge's own.

_GITHUB = github_api.GITHUB_API_BASE
_AZURE_VERSION = forge_repos._AZURE_VERSION
_repo = forge_repos.repo_api


def _branches(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> list[str]:
    repo = _repo(plan, segments)
    if plan.forge == "github":
        body = _call(client, "GET", f"{repo}/branches", deadline, ok=(200, 409), params={"per_page": 2})
        # 409 "Git Repository is empty." carries a message; anything else unread is never taken for empty.
        if isinstance(body, dict) and "empty" in str(body.get("message", "")).lower():
            return []
    elif plan.forge == "gitlab":
        project = _call(client, "GET", repo, deadline, ok=(200,))
        if not isinstance(project, dict):
            raise ForgeWriteError("GitLab answered something that is not a project.")
        if project.get("empty_repo") is True:
            return []
        body = _call(client, "GET", f"{repo}/repository/branches", deadline, ok=(200,), params={"per_page": 2})
    elif plan.forge == "bitbucket":
        page = _call(client, "GET", f"{repo}/refs/branches", deadline, ok=(200,), params={"pagelen": 2})
        body = page.get("values") if isinstance(page, dict) else None
    elif plan.forge == "azure_devops":
        page = _call(client, "GET", f"{repo}/refs", deadline, ok=(200,),
                     params={"filter": "heads/", "$top": 2, **_AZURE_VERSION})
        body = page.get("value") if isinstance(page, dict) else None
    else:
        raise KeyError(plan.forge)
    if not isinstance(body, list):
        raise ForgeWriteError(f"{forges.FORGE_LABELS[plan.forge]} answered something that is not a branch list.")
    names = [name for name in (forge_repos._str(b, "name") for b in body) if name]
    # Azure names each ref in full.
    return [name.removeprefix("refs/heads/") for name in names] if plan.forge == "azure_devops" else names


# --- GitHub -------------------------------------------------------------------------------------------------

def _github_init(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, files: list[tuple[str, str]],
) -> None:
    repo = _repo(plan, segments)
    (first_path, first_text), rest = files[0], files[1:]
    # The first commit lands on the repository's own default branch (a branch cannot be named before one exists),
    # renamed to main when it is something else.
    _github_put(client, repo, deadline, first_path, first_text, None)
    default = forge_repos._str(_call(client, "GET", repo, deadline, ok=(200,)), "default_branch")
    if default and default != INIT_BRANCH:
        _call(client, "POST", f"{repo}/branches/{forges.quote_segment(default)}/rename", deadline, ok=(201,),
              json={"new_name": INIT_BRANCH})
    for path, text in rest:
        _github_put(client, repo, deadline, path, text, INIT_BRANCH)


def _github_put(client: httpx.Client, repo: str, deadline: float, path: str, text: str, branch: Optional[str]) -> None:
    body: dict[str, Any] = {"message": f"Add {path}", "content": base64.b64encode(text.encode()).decode()}
    if branch is not None:
        body["branch"] = branch
    _call(client, "PUT", f"{repo}/contents/{forges.quote_segment(path)}", deadline, ok=(201,), json=body)


def _github_finish(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> None:
    repo = _repo(plan, segments)
    main = _call(client, "GET", f"{repo}/git/ref/heads/{INIT_BRANCH}", deadline, ok=(200,))
    sha = forge_repos._str(main, "object", "sha")
    if sha is None:
        raise ForgeWriteError(f"GitHub did not say where {INIT_BRANCH} is.")
    _call(client, "POST", f"{repo}/git/refs", deadline, ok=(201,),
          json={"ref": f"refs/heads/{INTEGRATION_BRANCH}", "sha": sha})
    # Only when main is not the default already: the PATCH needs "Administration: write", which a GitHub App's
    # user token lacks unless the App asks for it (optional), so an unconditional one fails a finished init.
    if forge_repos._str(_call(client, "GET", repo, deadline, ok=(200,)), "default_branch") != INIT_BRANCH:
        _call(client, "PATCH", repo, deadline, ok=(200,), json={"default_branch": INIT_BRANCH})


def _github_create(client: httpx.Client, plan: forge_repos._Plan, owner: str, name: str, deadline: float) -> str:
    login = forge_repos._str(_call(client, "GET", f"{_GITHUB}/user", deadline, ok=(200,)), "login")
    url = f"{_GITHUB}/user/repos" if login and login.lower() == owner.lower() else f"{_GITHUB}/orgs/{forges.quote_segment(owner)}/repos"
    try:
        body = _call(client, "POST", url, deadline, ok=(201,), json={"name": name, "private": True, "auto_init": False})
    except ForgeWriteError as exc:
        # Only GitHub's integration refusal: a person refused in their own right (an org that lets members create
        # no repository) keeps GitHub's own sentence, which says so.
        if exc.status == 403 and github_oauth.is_app_issued(plan.login.token) and _INTEGRATION_REFUSED in str(exc):
            raise ForgeWriteError(GITHUB_APP_CANNOT_CREATE, exc.status) from exc
        raise
    url = forge_repos._str(body, "html_url") or f"https://github.com/{owner}/{name}"
    if github_oauth.is_app_issued(plan.login.token):
        try:
            _github_grant(client, owner, body.get("id") if isinstance(body, dict) else None, deadline)
        except ForgeWriteError as exc:
            raise _GrantFailed(url, str(exc)) from exc
        except httpx.HTTPError as exc:
            # Made already: never the create's "could not be reached", whose retry would make it again.
            raise _GrantFailed(url, "GitHub could not be reached") from exc
    return url


def _github_grant(client: httpx.Client, owner: str, repo_id: Any, deadline: float) -> None:
    """Add the new repository `repo_id` to the App's installation on `owner` when that installation has "Only select
    repositories"; one on "All repositories" already reaches it."""
    installation = forge_repos.github_installations(client, deadline).get(owner.lower())
    if installation is None or installation.get("repository_selection") != "selected":
        return
    _github_add(client, installation, repo_id, deadline)


def _github_add(client: httpx.Client, installation: dict[str, Any], repo_id: Any, deadline: float) -> None:
    installation_id = installation.get("id")
    if any(not isinstance(i, int) or isinstance(i, bool) for i in (installation_id, repo_id)):
        raise ForgeWriteError("GitHub did not name the installation or the new repository.")
    _call(client, "PUT", f"{_GITHUB}/user/installations/{installation_id}/repositories/{repo_id}", deadline, ok=(204,))


def _github_app_refused(plan: forge_repos._Plan, exc: ForgeWriteError) -> bool:
    """Whether a GitHub App user token was refused a repository it may lack: GitHub's integration 403, or the 404 a
    private repository outside the installation answers."""
    if plan.forge != "github" or not github_oauth.is_app_issued(plan.login.token):
        return False
    return exc.status == 404 or (exc.status == 403 and _INTEGRATION_REFUSED in str(exc))


def _github_unwritten(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> bool:
    """Whether the repository still has no branch, or cannot be read at all (a private one outside the installation)."""
    try:
        return not _branches(client, plan, segments, deadline)
    except ForgeWriteError as exc:
        return exc.status in (403, 404)


def _github_reach(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, refused: ForgeWriteError,
) -> None:
    """Give the App's installation on the owner the repository `segments` names, or raise the one step that would:
    install the App there, or add the repository to it on GitHub. `refused` when the installation reaches every
    repository already: the refusal was not about reach. A private repository outside the installation cannot be
    read for its id, so it always gets the add-it-on-GitHub step; so does a URL naming no repository, which GitHub
    answers the same way."""
    owner, name = segments[0], "/".join(segments)
    installation = forge_repos.github_installations(client, deadline).get(owner.lower())
    if installation is None:
        raise GitHubAppNotInstalled(owner)
    if installation.get("repository_selection") != "selected":
        raise refused
    try:
        repo = _call(client, "GET", _repo(plan, segments), deadline, ok=(200,))
        _github_add(client, installation, repo.get("id") if isinstance(repo, dict) else None, deadline)
    except ForgeWriteError as exc:
        if exc.status not in (403, 404, 422):
            raise  # a rate limit or an SSO demand carries its own fix
        configure = _github_configure_url(installation)
        where = f" ({configure})" if configure else ""
        raise ForgeWriteError(f"Add {name} to the GitHub App's repository access on GitHub{where}, then try again.",
                              exc.status) from exc


def _github_configure_url(installation: dict[str, Any]) -> Optional[str]:
    """The installation's own Configure page on GitHub, where its repository access is edited; None without an id."""
    installation_id = installation.get("id")
    if not isinstance(installation_id, int) or isinstance(installation_id, bool):
        return None
    if forge_repos._str(installation, "account", "type") == "Organization":
        org = forge_repos._str(installation, "account", "login") or ""
        return f"https://github.com/organizations/{forges.quote_segment(org)}/settings/installations/{installation_id}"
    return f"https://github.com/settings/installations/{installation_id}"


# --- GitLab -------------------------------------------------------------------------------------------------

def _gitlab_init(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, files: list[tuple[str, str]],
) -> None:
    _call(client, "POST", f"{_repo(plan, segments)}/repository/commits", deadline, ok=(201,), json={
        "branch": INIT_BRANCH, "commit_message": "Initialize repository",
        "actions": [{"action": "create", "file_path": path, "content": text} for path, text in files],
    })


def _gitlab_finish(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> None:
    project = _repo(plan, segments)
    _call(client, "POST", f"{project}/repository/branches", deadline, ok=(201,),
          params={"branch": INTEGRATION_BRANCH, "ref": INIT_BRANCH})
    _call(client, "PUT", project, deadline, ok=(200,), json={"default_branch": INIT_BRANCH})


def _gitlab_create(client: httpx.Client, plan: forge_repos._Plan, owner: str, name: str, deadline: float) -> str:
    api = f"https://{plan.root}/api/v4"
    namespace = _call(client, "GET", f"{api}/namespaces/{forges.quote_segment(owner)}", deadline, ok=(200,))
    namespace_id = namespace.get("id") if isinstance(namespace, dict) else None
    if not isinstance(namespace_id, int):
        raise ForgeWriteError(f"GitLab has no group or user {owner} this connection can create in.")
    body = _call(client, "POST", f"{api}/projects", deadline, ok=(201,), json={
        "name": name, "path": name, "namespace_id": namespace_id, "visibility": "private",
    })
    return forge_repos._str(body, "web_url") or f"https://{plan.root}/{owner}/{name}"


# --- Bitbucket ----------------------------------------------------------------------------------------------

def _bitbucket_init(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, files: list[tuple[str, str]],
) -> None:
    repo = _repo(plan, segments)
    # `src` commits on top of an existing branch, overwriting the files it names, and `parents` cannot say "no
    # parent": emptiness is read again right before the write, and the commit checked to be the first after it.
    if _branches(client, plan, segments, deadline):
        raise ForgeWriteError("Someone pushed to this repository while Jaah was initializing it; it was left as it is.")
    _call(client, "POST", f"{repo}/src", deadline, ok=(200, 201),
          data={"message": "Initialize repository", "branch": INIT_BRANCH},
          files=[(path, (path, text.encode())) for path, text in files])
    main = _call(client, "GET", f"{repo}/refs/branches/{INIT_BRANCH}", deadline, ok=(200,))
    target = main.get("target") if isinstance(main, dict) else None
    if isinstance(target, dict) and target.get("parents"):
        raise ForgeWriteError(
            f"Someone pushed to this repository while Jaah was initializing it: README.md and .gitignore landed on "
            f"top of their {INIT_BRANCH}. Check it on Bitbucket.")


def _bitbucket_finish(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> None:
    repo = _repo(plan, segments)
    main = _call(client, "GET", f"{repo}/refs/branches/{INIT_BRANCH}", deadline, ok=(200,))
    sha = forge_repos._str(main, "target", "hash")
    if sha is None:
        raise ForgeWriteError(f"Bitbucket did not say where {INIT_BRANCH} is.")
    _call(client, "POST", f"{repo}/refs/branches", deadline, ok=(200, 201),
          json={"name": INTEGRATION_BRANCH, "target": {"hash": sha}})
    current = _call(client, "GET", repo, deadline, ok=(200,))
    if forge_repos._str(current, "mainbranch", "name") != INIT_BRANCH:
        _call(client, "PUT", repo, deadline, ok=(200,), json={"mainbranch": {"name": INIT_BRANCH}})


def _bitbucket_create(client: httpx.Client, plan: forge_repos._Plan, owner: str, name: str, deadline: float) -> str:
    slug = name.lower()
    body = _call(client, "POST", _repo(plan, [owner, slug]), deadline, ok=(200, 201),
                 json={"scm": "git", "is_private": True, "name": name})
    return forge_repos._str(body, "links", "html", "href") or f"https://bitbucket.org/{owner}/{slug}"


# --- Azure DevOps -------------------------------------------------------------------------------------------

def _azure_project_api(org: str, project: str) -> str:
    return f"https://dev.azure.com/{forges.quote_segment(org)}/{forges.quote_segment(project)}/_apis/git/repositories"


def _azure_init(
    client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float, files: list[tuple[str, str]],
) -> None:
    _call(client, "POST", f"{_repo(plan, segments)}/pushes", deadline, ok=(200, 201), params=_AZURE_VERSION, json={
        "refUpdates": [{"name": f"refs/heads/{INIT_BRANCH}", "oldObjectId": _ZERO_SHA}],
        "commits": [{
            "comment": "Initialize repository",
            "changes": [
                {"changeType": "add", "item": {"path": f"/{path}"}, "newContent": {"content": text, "contentType": "rawtext"}}
                for path, text in files
            ],
        }],
    })


def _azure_finish(client: httpx.Client, plan: forge_repos._Plan, segments: list[str], deadline: float) -> None:
    repo = _repo(plan, segments)
    main = _call(client, "GET", f"{repo}/refs", deadline, ok=(200,),
                 params={"filter": f"heads/{INIT_BRANCH}", **_AZURE_VERSION})
    values = main.get("value") if isinstance(main, dict) else None
    sha = next((forge_repos._str(r, "objectId") for r in values or []
                if forge_repos._str(r, "name") == f"refs/heads/{INIT_BRANCH}"), None)
    if sha is None:
        raise ForgeWriteError(f"Azure DevOps did not say where {INIT_BRANCH} is.")
    made = _call(client, "POST", f"{repo}/refs", deadline, ok=(200, 201), params=_AZURE_VERSION, json=[
        {"name": f"refs/heads/{INTEGRATION_BRANCH}", "oldObjectId": _ZERO_SHA, "newObjectId": sha},
    ])
    results = made.get("value") if isinstance(made, dict) else None
    # A 200 reports each ref's own outcome.
    if not isinstance(results, list) or not all(isinstance(r, dict) and r.get("success") for r in results):
        raise ForgeWriteError(f"Azure DevOps did not create {INTEGRATION_BRANCH}.")
    current = _call(client, "GET", repo, deadline, ok=(200,), params=_AZURE_VERSION)
    repo_id = forge_repos._str(current, "id")
    if forge_repos._str(current, "defaultBranch") != f"refs/heads/{INIT_BRANCH}" and repo_id:
        org, project = segments[:2]
        _call(client, "PATCH", f"{_azure_project_api(org, project)}/{forges.quote_segment(repo_id)}", deadline, ok=(200,),
              params=_AZURE_VERSION, json={"defaultBranch": f"refs/heads/{INIT_BRANCH}"})


def _azure_create(client: httpx.Client, plan: forge_repos._Plan, owner: str, name: str, deadline: float) -> str:
    org, project = (urllib.parse.unquote(part) for part in owner.split("/"))  # `create` checked the parts
    found = _call(client, "GET", f"https://dev.azure.com/{forges.quote_segment(org)}/_apis/projects/{forges.quote_segment(project)}", deadline, ok=(200,),
                  params=_AZURE_VERSION)
    project_id = forge_repos._str(found, "id")
    if project_id is None:
        raise ForgeWriteError(f"Azure DevOps has no project {owner} this connection can create in.")
    body = _call(client, "POST", _azure_project_api(org, project), deadline, ok=(200, 201), params=_AZURE_VERSION,
                 json={"name": name, "project": {"id": project_id}})
    return forge_repos._str(body, "webUrl") or f"https://dev.azure.com/{forges.quote_segment(org)}/{forges.quote_segment(project)}/_git/{forges.quote_segment(name)}"


_INIT = {"github": _github_init, "gitlab": _gitlab_init, "bitbucket": _bitbucket_init, "azure_devops": _azure_init}
_FINISH = {
    "github": _github_finish, "gitlab": _gitlab_finish, "bitbucket": _bitbucket_finish, "azure_devops": _azure_finish,
}
_CREATE = {
    "github": _github_create, "gitlab": _gitlab_create, "bitbucket": _bitbucket_create, "azure_devops": _azure_create,
}
