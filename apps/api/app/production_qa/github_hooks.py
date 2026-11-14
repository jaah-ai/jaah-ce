"""GitHub deploy wiring for Connect deploys (`connect.py`): the single home of "a GitHub token that may act on this
project's repository" (`repo_token`), of installing the deploy webhooks with it (`install`), and of finding the
workflows that deploy the Production branch (`detect_workflows`).

`repo_token` tries, first that works wins:
  1. `app` — the console's GitHub App installation on the repository, minted for that one repository with only the
     permission the purpose needs (`easy_mode_runner_tokens.app_installation_token`). The App's key signs for every
     org that installed it and a project's repository URL is unverified text, so the App acts only where someone is
     proven, by their own connected login, to hold the role the purpose needs on the repository (admin for `hooks`,
     read for `contents` and `runs`): an Owner/Admin of the project's own org, or the caller. Without that proof a
     tenant could wire webhooks into another org's repository. A public repository is read with the login itself, never the App.
  2. `member` — an org Owner's or Admin's connected GitHub login that may manage the repository's webhooks (`GET
     /repos/{o}/{r}/hooks` answers 200), or read it for `contents` or `runs` — the people whose logins already run the
     project's background work (`git_access.project_forge_person`). A member's role on the repository never counts.
  3. `clicker` — the caller's own login, the same check.

A token never leaves this process: never logged, never answered. `source` is logged. Every GitHub failure is a
refusal or an empty answer here, never an exception to the route.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import quote
from uuid import UUID

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forges, git_access, github_api
from app.models.automations import TaskTrigger
from app.models.credentials import Credential, UserGitConnection
from app.models.identity import AdminUser
from app.models.projects import Project
from app.triggers import SOURCE_GITHUB

log = logging.getLogger("production-qa.github-hooks")

Purpose = Literal["hooks", "contents", "runs"]
Source = Literal["app", "member", "clicker"]
RefusedReason = Literal["no_reach", "no_permission"]

#: What an App installation token is minted with, per purpose: nothing more.
_APP_PERMISSIONS: dict[str, dict[str, str]] = {"hooks": {"repository_hooks": "write"}, "contents": {"contents": "read"},
                                                "runs": {"actions": "read"}}
#: The repository role (`GET /repos/{o}/{r}` → `permissions`) an org member must hold for the App to act, per purpose.
_ROLE = {"hooks": "admin", "contents": "pull", "runs": "pull"}
#: The most connected members probed per resolve: each costs a GitHub read.
_MAX_PEOPLE = 10
_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=2.0)
_WORKFLOWS_DIR = ".github/workflows"
#: The most workflow files read per detection, the largest one parsed, and the bounds on one branch filter: a
#: tenant writes these files, and they are parsed on the shared API.
_MAX_WORKFLOWS = 30
_MAX_WORKFLOW_BYTES = 256 * 1024
_MAX_PATTERNS = 100
_MAX_PATTERN_CHARS = 255


@dataclass(frozen=True)
class RepoToken:
    token: str
    source: Source
    owner_repo: str

    def __repr__(self) -> str:  # never the token
        return f"RepoToken(source={self.source!r}, owner_repo={self.owner_repo!r})"


@dataclass(frozen=True)
class InstallResult:
    state: Literal["installed", "refused"]
    reason: Optional[RefusedReason] = None
    #: The repository it was for (`owner/repo`), when the project has one on GitHub.
    owner_repo: Optional[str] = None


@dataclass(frozen=True)
class Detection:
    #: The deploy workflows Connect prefills; `optional`: the other candidates, offered but not prefilled.
    workflows: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    pages_branch_built: bool = False
    readable: bool = False


@dataclass
class _Person:
    token: str = field(repr=False)
    source: Source = "member"
    user_id: Optional[UUID] = None
    status: int = 0
    repo: dict[str, Any] = field(default_factory=dict)


def github_repository(session: Session, project: Project) -> Optional[str]:
    """`owner/repo` of the GitHub repository Connect deploys wires, or None when the project's deploy forge is not
    GitHub (`connect.deploy_forge`)."""
    from app.production_qa.connect import deploy_forge

    forge, repository = deploy_forge(session, project)
    return repository if forge == SOURCE_GITHUB else None


def repo_token(session: Session, project: Project, *, purpose: Purpose,
               actor_id: Optional[UUID] = None) -> Optional[RepoToken]:
    """The first token that may act on the project's GitHub repository for `purpose` (module docstring), or None.
    `actor_id`: the person asking, tried last as `clicker`."""
    return _resolve(session, project, purpose, actor_id)[0]


def _resolve(session: Session, project: Project, purpose: Purpose,
             actor_id: Optional[UUID]) -> tuple[Optional[RepoToken], bool]:
    """`(token, reached)`: `reached` is whether some org member's login holds the purpose's role on the repository —
    the difference between "nobody here reaches it" and "reached, but nothing may do it"."""
    owner_repo = github_repository(session, project)
    if owner_repo is None:
        return None, False
    people = _people(session, project, owner_repo, actor_id)
    for person in people:
        person.status, person.repo = _read_repo(person.token, owner_repo)
    role = _ROLE[purpose]
    proven = [p for p in people if p.status == 200 and (p.repo.get("permissions") or {}).get(role) is True]
    # A public repository answers `pull` to anyone: read it with the login itself, never through someone's installation.
    app_allowed = bool(proven) and (purpose == "hooks" or proven[0].repo.get("private") is True)
    if app_allowed:
        from app.easy_mode_runner_tokens import app_installation_token

        repo_id = proven[0].repo.get("id")
        if isinstance(repo_id, int):
            token = app_installation_token(owner_repo, repo_id, _APP_PERMISSIONS[purpose])
            if token:
                log.info("github deploy wiring: purpose=%s repo=%s source=app proven_by=%s", purpose, owner_repo,
                         proven[0].user_id)
                return RepoToken(token=token, source="app", owner_repo=owner_repo), True
    for person in proven:
        if purpose != "hooks" or _can_manage_hooks(person.token, owner_repo):
            log.info("github deploy wiring: purpose=%s repo=%s source=%s user=%s", purpose, owner_repo, person.source,
                     person.user_id)
            return RepoToken(token=person.token, source=person.source, owner_repo=owner_repo), True
    log.info("github deploy wiring: purpose=%s repo=%s refused people=%d reached=%s", purpose, owner_repo,
             len(people), bool(proven))
    return None, bool(proven)


def _people(session: Session, project: Project, owner_repo: str, actor_id: Optional[UUID]) -> list[_Person]:
    """The org's live Owners/Admins holding a live GitHub login on the repository's root, then the caller
    (`clicker`); each with the token their own connection selects for the repository."""
    from app.access import owners_and_admins

    if project.org_id is None:
        return []
    repo = forges.parse_repo_url(f"https://github.com/{owner_repo}", SOURCE_GITHUB)
    connected = list(dict.fromkeys(session.scalars(
        select(UserGitConnection.user_id)
        .join(Credential, Credential.id == UserGitConnection.credential_id)
        .join(AdminUser, AdminUser.id == UserGitConnection.user_id)
        .where(UserGitConnection.root == repo.root, Credential.needs_reconnect_at.is_(None),
               AdminUser.org_id == project.org_id, AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None))
        .order_by(UserGitConnection.created_at)
    )))
    admins = {u.id for u in owners_and_admins(session, project.org_id)}
    ordered = [u for u in connected if u != actor_id and u in admins][:_MAX_PEOPLE]
    if actor_id is not None and actor_id in connected:
        ordered.append(actor_id)
    out: list[_Person] = []
    for user_id in ordered:
        try:
            credential = git_access.resolve_for_user(session, user_id, repo)
            token = git_access.rest_login_for(session, credential).token
        except git_access.GitAccessError:
            continue
        if token:
            out.append(_Person(token=token, source="clicker" if user_id == actor_id else "member", user_id=user_id))
    return out


def _path(owner_repo: str, tail: str = "") -> str:
    owner, _, name = owner_repo.partition("/")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{tail}"


def _read_repo(token: str, owner_repo: str) -> tuple[int, dict[str, Any]]:
    try:
        status, body = github_api.repo_json(token, owner_repo, timeout=_TIMEOUT)
    except (httpx.HTTPError, ValueError):
        return 0, {}
    return status, body if isinstance(body, dict) else {}


def _can_manage_hooks(token: str, owner_repo: str) -> bool:
    try:
        status, _body = github_api.get_json(token, _path(owner_repo, "/hooks"), timeout=_TIMEOUT)
    except (httpx.HTTPError, ValueError):
        return False
    return status == 200


def hooks_token(session: Session, project: Project, *,
                actor_id: Optional[UUID] = None) -> tuple[Optional[RepoToken], Optional[InstallResult]]:
    """The token `install` writes with, or the refusal to answer instead — asked BEFORE any trigger is created, so a
    connect nothing can install leaves nothing behind: `no_reach` when no Owner/Admin login nor the caller's is admin
    on the repository, `no_permission` when one is but neither the App nor any login may manage its webhooks."""
    token, reached = _resolve(session, project, "hooks", actor_id)
    if token is not None:
        return token, None
    return None, InstallResult("refused", "no_permission" if reached else "no_reach",
                               github_repository(session, project))


def install(session: Session, project: Project, triggers: list[TaskTrigger], *,
            actor_id: Optional[UUID] = None, token: Optional[RepoToken] = None) -> InstallResult:
    """Install or update one GitHub webhook per deploy trigger, matched by its URL: a re-Connect updates the hook
    (events, secret, active) and never duplicates it. The secret is the trigger's own, opened here. `token`: one
    `hooks_token` already resolved, else resolved here. `refused` as `hooks_token`, or `no_permission` when GitHub
    refused a write."""
    from app.production_qa.connect import events_of
    from app.routes.triggers import _url_for
    from app import trigger_secrets

    if token is None:
        token, refused = hooks_token(session, project, actor_id=actor_id)
        if token is None:
            return refused or InstallResult("refused", "no_reach")
    try:
        with github_api.client(token.token, timeout=_TIMEOUT) as http:
            listed = http.get(_path(token.owner_repo, "/hooks"), params={"per_page": 100})
            if listed.status_code != 200:
                return _refused(token, f"list {listed.status_code}")
            existing = {str((h.get("config") or {}).get("url") or ""): h.get("id")
                        for h in listed.json() if isinstance(h, dict)}
            for trigger in triggers:
                url = _url_for(trigger)
                if not url or not trigger.secret_sealed:
                    return _refused(token, "trigger has no ingress url or secret")
                body = {"active": True, "events": events_of(trigger), "config": {
                    "url": url, "content_type": "json", "insecure_ssl": "0",
                    "secret": trigger_secrets.open_secret(trigger),
                }}
                hook_id = existing.get(url)
                resp = (http.patch(_path(token.owner_repo, f"/hooks/{int(hook_id)}"), json=body)
                        if isinstance(hook_id, int) else http.post(_path(token.owner_repo, "/hooks"),
                                                                    json={"name": "web", **body}))
                if resp.status_code not in (200, 201):
                    return _refused(token, f"write {resp.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        return _refused(token, type(exc).__name__)
    from app.clock import utcnow

    for trigger in triggers:
        # Installed: the secret is GitHub's now, so its one reveal is spent (`reveal_owed` never hands it out; a
        # trigger still owing a reveal is one no hook carries — `setup_owed`).
        if trigger.secret_revealed_at is None:
            trigger.secret_revealed_at = utcnow()
    session.flush()
    log.info("github deploy wiring: installed %d hooks repo=%s source=%s", len(triggers), token.owner_repo,
             token.source)
    return InstallResult("installed", owner_repo=token.owner_repo)


def _refused(token: RepoToken, why: str) -> InstallResult:
    log.warning("github deploy wiring: install refused repo=%s source=%s (%s)", token.owner_repo, token.source, why)
    return InstallResult("refused", "no_permission", token.owner_repo)


# ── Workflow detection ──────────────────────────────────────────────────────────────────────────────

def detect_workflows(session: Session, project: Project, production_branch: str, *,
                     actor_id: Optional[UUID] = None) -> Detection:
    """The `.github/workflows/*.yml|yaml` files on `production_branch` that deploy it (`deploys`): run on a push to
    it and ship a GitHub environment. One candidate is prefilled; of several, the one with the most successful push
    runs on the branch (`_deployed_by`), the rest `optional` — none prefilled when no run proves one. `pages_branch_built`: the repository publishes GitHub Pages and no workflow matched (Pages
    builds from the branch, with no workflow file to name). Any read failure: `readable=False` and nothing found."""
    token = repo_token(session, project, purpose="contents", actor_id=actor_id)
    if token is None:
        return Detection()
    try:
        status, repo = github_api.repo_json(token.token, token.owner_repo, timeout=_TIMEOUT)
        status_dir, listing = github_api.get_json(token.token, _path(token.owner_repo, f"/contents/{_WORKFLOWS_DIR}"),
                                                  params={"ref": production_branch}, timeout=_TIMEOUT)
        if status != 200 or status_dir not in (200, 404):
            return Detection()
        names = sorted(str(e.get("path") or "") for e in (listing if isinstance(listing, list) else [])
                       if isinstance(e, dict) and e.get("type") == "file"
                       and str(e.get("name") or "").endswith((".yml", ".yaml")))[:_MAX_WORKFLOWS]
        found = [path for path in names
                 if deploys(_parsed(github_api.get_text(token.owner_repo, quote(path, safe="/"), production_branch,
                                                        token=token.token, timeout=_TIMEOUT)), production_branch)]
        branch_built = not found and isinstance(repo, dict) and repo.get("has_pages") is True \
            and _pages_build_type(token) != "workflow"
    except (httpx.HTTPError, ValueError):
        return Detection()
    chosen = found if len(found) <= 1 else _deployed_by(session, project, token, production_branch, found, actor_id)
    return Detection(workflows=chosen, optional=[p for p in found if p not in chosen], pages_branch_built=branch_built,
                     readable=True)


def _deployed_by(session: Session, project: Project, token: RepoToken, branch: str, candidates: list[str],
                 actor_id: Optional[UUID]) -> list[str]:
    """The one candidate with the most successful push runs among the branch's latest 100, or none when no run proves
    one or GitHub cannot be read. Every candidate ships an environment (`deploys`), so a successful push run on the
    branch is one that deployed it. An App token was minted for contents alone, so the runs are read with one minted
    for them (`runs`)."""
    reader = token if token.source != "app" else repo_token(session, project, purpose="runs", actor_id=actor_id)
    if reader is None:
        return []
    try:
        status, body = github_api.get_json(reader.token, _path(reader.owner_repo, "/actions/runs"), timeout=_TIMEOUT,
                                           params={"branch": branch, "event": "push", "status": "success",
                                                   "per_page": 100})
    except (httpx.HTTPError, ValueError):
        return []
    runs = body.get("workflow_runs") if status == 200 and isinstance(body, dict) else None
    counts = Counter(path for path in (str(run.get("path") or "").partition("@")[0]
                                       for run in (runs if isinstance(runs, list) else []) if isinstance(run, dict))
                     if path in candidates)
    if not counts:
        return []
    return [min(counts, key=lambda path: (-counts[path], path))]


def deploys(doc: Any, branch: str) -> bool:
    """Whether a workflow document deploys `branch`: it runs on a push to it (`pushes_to`) and a job ships a GitHub
    environment (`environment:` a name, or a mapping naming one). A review or test workflow ships none."""
    if not pushes_to(doc, branch):
        return False
    jobs = doc.get("jobs")
    return isinstance(jobs, dict) and any(_environment_of(job) for job in list(jobs.values())[:_MAX_PATTERNS])


def _environment_of(job: Any) -> bool:
    environment = job.get("environment") if isinstance(job, dict) else None
    if isinstance(environment, dict):
        environment = environment.get("name")
    return isinstance(environment, str) and bool(environment.strip())


def _pages_build_type(token: RepoToken) -> Optional[str]:
    """`legacy` (built from the branch) or `workflow` (an Actions workflow deploys it); None when unreadable (an
    installation token without Pages access), read as branch-built since no workflow matched."""
    status, body = github_api.get_json(token.token, _path(token.owner_repo, "/pages"), timeout=_TIMEOUT)
    return str(body.get("build_type")) if status == 200 and isinstance(body, dict) else None


def _parsed(text: Optional[str]) -> Any:
    if not text or len(text) > _MAX_WORKFLOW_BYTES:
        return None
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError):  # a deeply nested file overflows the parser's stack
        return None


def pushes_to(doc: Any, branch: str) -> bool:
    """Whether a workflow document runs on a push to `branch`: `on` is `push`, a list holding it, or a mapping whose
    `push` names no branch filter, or one that `branch` passes (GitHub's globs and `!` negations; `branches-ignore`).
    A push filtered to tags only never runs on a branch push. YAML 1.1 reads a bare `on:` key as `True`."""
    if not isinstance(doc, dict):
        return False
    on = doc.get("on", doc.get(True))
    if on == "push":
        return True
    if isinstance(on, list):
        return "push" in on
    if not isinstance(on, dict) or "push" not in on:
        return False
    push = on["push"]
    if not isinstance(push, dict):
        return True
    if push.get("branches") is not None:
        return _passes(_strings(push["branches"]), branch)
    if push.get("branches-ignore") is not None:
        return not any(_glob(p, branch) for p in _strings(push["branches-ignore"]))
    return push.get("tags") is None and push.get("tags-ignore") is None


def _strings(value: Any) -> list[str]:
    """The string patterns of a filter, scalars only: never `str()` of a container (an alias bomb expands there)."""
    return [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)][:_MAX_PATTERNS]


def _passes(patterns: list[str], branch: str) -> bool:
    """GitHub's `branches` rule: the last pattern that matches decides; a `!` pattern excludes."""
    verdict = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if _glob(pattern[1:], branch):
                verdict = False
        elif _glob(pattern, branch):
            verdict = True
    return verdict


def _glob(pattern: str, name: str) -> bool:
    """GitHub's filter glob: `**` any characters, `*` any but `/`, `?` the previous character optional, `+` one or
    more of it; the rest literal. Matched by a position-set walk, never a backtracking regex: a tenant writes both the
    pattern and the branch, so the cost is bounded (pattern × branch², both capped)."""
    if len(pattern) > _MAX_PATTERN_CHARS or len(name) > _MAX_PATTERN_CHARS:
        return False
    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            tokens.append(("any", ""))
            i += 2
            while i < len(pattern) and pattern[i] == "*":
                i += 1
        elif ch == "*":
            tokens.append(("seg", ""))
            i += 1
        elif i + 1 < len(pattern) and pattern[i + 1] in "?+":
            tokens.append(("opt" if pattern[i + 1] == "?" else "plus", ch))
            i += 2
        else:
            tokens.append(("lit", ch))
            i += 1
    n = len(name)
    at = {0}
    for kind, ch in tokens:
        if not at:
            return False
        if kind == "any":
            at = set(range(min(at), n + 1))
        elif kind == "seg":
            nxt: set[int] = set()
            for start in sorted(at):
                j = start
                nxt.add(j)
                while j < n and name[j] != "/" and j + 1 not in nxt:
                    j += 1
                    nxt.add(j)
            at = nxt
        else:
            one = {j + 1 for j in at if j < n and name[j] == ch}
            if kind == "plus":
                frontier = set(one)
                while frontier:
                    frontier = {j + 1 for j in frontier if j < n and name[j] == ch} - one
                    one |= frontier
            at = one | at if kind == "opt" else one
    return n in at
