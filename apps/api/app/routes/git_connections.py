"""Git Access per forge: a person's git connections, one per forge root.

`GET/PUT/DELETE /api/users/{user_id}/git-connections[/{root}]` and `POST …/{root}/pat`. `{root}` is the
URL-encoded `forges.RepoRef.root`. `GET /api/git-repos` lists the repositories those connections reach, for
the Repo URL lookup (`app/forge_repos.py`), and `GET /api/git-repos/default-branch` one repository's default
branch, for the Integration branch prefill, and whether it is empty; `GET /api/git-repos/owners` what a new
repository can be created in. The create wizard's two writes — `POST
/api/git-repos` (a new private repository) and `POST /api/git-repos/initialize` (main and develop for an empty
one) — go through the same connection (`app/forge_repo_init.py`). Store and overlap rules: `app/git_connections.py`; resolution:
`app/git_access.py`. SELF-SERVICE ONLY: a person reads and writes their own connections, exactly as the
Profile Git Access pointer is theirs (`routes/auth.py::MeUpdate.git_credential_id`); another user's id is
a 404. Every route declares `own_credential` over a `credentials:*` key, whose only enforcement is the
handler's own narrowing: the self check below, and `git_credential_scope` for the credential a PUT names.

The PAT body carries a live token, so `app/user_activity.py` stores no body for these paths, and the
body is read as plain JSON so a malformed field is refused without echoing it (pydantic's 422 would).
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import forge_repo_init, forge_repos, forges, git_access, git_connections, git_probe
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.credentials.registry import type_label
from app.credentials.testers import AZURE_DEVOPS_KINDS, BITBUCKET_KINDS, new_deadline
from app.db import get_session
from app.forges import FORGE_LABELS
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.routes.auth import GitCredentialOut, git_credential_scope
from app.routes.forge_connect import connect_offer
from app.routes.git_connect import install_url

log = logging.getLogger("app.routes.git_connections")

router = APIRouter(tags=["git-connections"])

_PREFIX = "/api/users/{user_id}/git-connections"
# Pasted tokens are bounded like every credential value (`routes/credentials/credential_wire.py::_VALUE_MAX_LEN`).
_TOKEN_MAX = 4096


class GitConnectOffer(BaseModel):
    """Whether this root offers a working Connect (OAuth) button (`routes/forge_connect.py::connect_offer`):
    `false` with `reason` naming what to configure until the forge's app is designated, and always for a
    self-managed GitLab (PAT-only, D4). A pasted token is offered either way."""

    available: bool = False
    reason: Optional[str] = None


class GitConnectionOut(BaseModel):
    root: str
    forge: str
    forge_label: str
    self_managed: bool
    state: Literal["connected", "needs_reconnect", "not_connected"]
    # Why the root is not usable, in resolution's own words (names forge, root, person and the fix).
    detail: Optional[str] = None
    credential: Optional[GitCredentialOut] = None
    connect: GitConnectOffer


class GitConnectionListOut(BaseModel):
    connections: list[GitConnectionOut]


class GitConnectionIn(BaseModel):
    credential_id: UUID


def _self_or_404(user_id: UUID, user: AdminUser) -> AdminUser:
    """The caller, when `user_id` is the caller; a 404 otherwise, so another person's connections are
    neither readable nor distinguishable from a missing user."""
    if user_id != user.id:
        raise HTTPException(status_code=404, detail="unknown user")
    return user


def _root_or_422(root: str) -> tuple[str, str]:
    """`(root, forge)` for a well-formed root, else a 422 naming the accepted shapes."""
    cleaned = (root or "").strip().lower()
    forge = forges.forge_of_root(cleaned)
    if forge is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Not a forge root. Use github.com, gitlab.com, bitbucket.org or dev.azure.com, or a "
                "self-managed GitLab as host[/sub-path] (for example git.example.org/gitlab)."
            ),
        )
    return cleaned, forge


def _out(entry: git_connections.ConnectionState, user: AdminUser) -> GitConnectionOut:
    credential = entry.credential
    available, reason = connect_offer(entry.root, user)
    return GitConnectionOut(
        root=entry.root,
        forge=entry.forge,
        forge_label=FORGE_LABELS[entry.forge],
        self_managed=entry.root not in forges.FIXED_ROOTS,
        state=entry.status,
        detail=entry.detail,
        credential=(
            GitCredentialOut(id=credential.id, name=credential.name, type=credential.type,
                             type_label=type_label(credential.type))
            if credential is not None else None
        ),
        connect=GitConnectOffer(available=available, reason=reason),
    )


@router.get(_PREFIX, response_model=GitConnectionListOut)
@feature("credentials", "view", own_credential=True)
def list_git_connections(
    user_id: UUID,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitConnectionListOut:
    """The four fixed roots plus each self-managed GitLab the person connected, with each one's state:
    `connected`, `needs_reconnect` (the forge reported the token revoked, D-A14 — stale connections are
    re-checked here, in parallel, under one 5 s bound) or `not_connected`."""
    subject = _self_or_404(user_id, user)
    states = git_connections.connection_states(session, subject)
    return GitConnectionListOut(connections=[_out(entry, subject) for entry in states])


@router.put(_PREFIX + "/{root:path}", response_model=GitConnectionOut)
@feature("credentials", "edit", own_credential=True)
def put_git_connection(
    user_id: UUID,
    root: str,
    body: GitConnectionIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitConnectionOut:
    """Connect an existing credential for `root`: one the person may push as (`git_credential_scope`, the
    Git Access dropdown's rule), of a git type, that authenticates exactly this root."""
    subject = _self_or_404(user_id, user)
    root, _forge = _root_or_422(root)
    credential = session.scalars(
        git_credential_scope(session, subject).where(Credential.id == body.credential_id)
    ).first()
    if credential is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "That credential is not available to this account — it does not exist, or the grant "
                "on it was revoked. Pick one of the credentials offered here."
            ),
        )
    if credential.type not in git_access.GIT_CREDENTIAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'{credential.name}' is a {type_label(credential.type)} credential and cannot "
                   "authenticate a git remote.",
        )
    credential_root = git_access.credential_root(credential)
    if credential_root != root:
        raise HTTPException(
            status_code=422,
            detail=f"'{credential.name}' authenticates {credential_root}, not {root}.",
        )
    git_connections.set_connection(session, subject, user, root, credential)
    session.commit()
    return _entry_for(session, subject, root)


@router.delete(_PREFIX + "/{root:path}", status_code=204)
@feature("credentials", "edit", own_credential=True)
def delete_git_connection(
    user_id: UUID,
    root: str,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Disconnect `root` alone; every other forge's connection is untouched. The credential itself is
    kept (it may be selected elsewhere); delete it under Directory > Secrets."""
    subject = _self_or_404(user_id, user)
    root, _forge = _root_or_422(root)
    git_connections.delete_connection(session, subject, root)
    session.commit()
    return Response(status_code=204)


def _pat_field(raw: dict[str, Any], key: str, *, required: bool) -> str:
    """One string field of the PAT body, refused by NAME only — never echoing its value."""
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise HTTPException(status_code=422, detail=f"{key} is required")
        return ""
    if not isinstance(value, str) or len(value) > _TOKEN_MAX or "\x00" in value:
        raise HTTPException(status_code=422, detail=f"{key} must be a string of at most {_TOKEN_MAX} characters")
    return value.strip()


def _pat_config(forge: str, root: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The credential config a pasted token becomes, per forge: GitLab's base URL is derived from the
    root (self-managed is PAT-only, D4); Bitbucket takes an API token with its account email (no
    app-password kind, D-A10); Azure DevOps a PAT with the organization it is tested against."""
    config: dict[str, Any] = {"token": _pat_field(raw, "token", required=True)}
    if forge == "gitlab" and root != "gitlab.com":
        config["base_url"] = f"https://{root}"
    elif forge == "bitbucket":
        config["username"] = _pat_field(raw, "username", required=True)
        config["kind"] = BITBUCKET_KINDS[0]
    elif forge == "azure_devops":
        config["test_org"] = _pat_field(raw, "test_org", required=True)
        config["kind"] = AZURE_DEVOPS_KINDS[0]
    return config


@router.post(_PREFIX + "/{root:path}/pat", response_model=GitConnectionOut, status_code=201)
@feature("credentials", "create", own_credential=True)
def post_git_connection_pat(
    user_id: UUID,
    root: str,
    raw: Any = Body(default=None),
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GitConnectionOut:
    """Connect `root` with a pasted token: tested against the forge first, and refused with the tester's
    own reason (422) when the forge does not accept it. On success the token is stored as the person's
    OWN credential (re-pasting for the same root replaces that row's token) and connected."""
    subject = _self_or_404(user_id, user)
    root, forge = _root_or_422(root)
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="the body must be a JSON object")
    config = _pat_config(forge, root, raw)
    if forge == "gitlab" and root != "gitlab.com":
        problem = git_probe.private_host_problem(root)
        if problem is not None:
            raise HTTPException(status_code=422, detail=f"The GitLab at {root} cannot be used: {problem}.")
    result = git_probe._run_tester(forge, dict(config), new_deadline())
    if not result.ok:
        log.info("git-connections: user=%s pasted a %s token for %s that the forge refused (%s)",
                 subject.id, forge, root, result.detail)
        raise HTTPException(status_code=422, detail=result.message)

    label = FORGE_LABELS[forge]
    credential = git_connections.own_row_for_root(
        session, subject, principal, forge, root, grant=False, account=result.identity
    )
    if credential is None:
        name = git_connections.free_name(
            session, subject, git_connections.connection_name(forge.replace("_", "-"), "token", root)
        )
        # Always personal, as Connect GitHub's row (`git_connections.connect_github`).
        credential = Credential(name=name, type=forge, description="", config={}, owner_id=subject.id)
        stamp_actor(credential, subject, created=True)
        session.add(credential)
        session.flush()
    else:
        stamp_actor(credential, subject)
    credential.description = (
        f"Pasted under Profile > Git > {label} and tested on save: {result.message}"
    )
    # A fresh dict: it replaces the old token and clears any recorded revocation (`grant_dead_at`).
    credential.config = config
    git_connections.stamp_account(credential, result.identity)
    git_connections.set_connection(session, subject, user, root, credential)
    session.commit()
    log.info("git-connections: user=%s connected %s with credential id=%s", subject.id, root, credential.id)
    return _entry_for(session, subject, root)


def _entry_for(session: Session, user: AdminUser, root: str) -> GitConnectionOut:
    """One root's state after a write, without re-checking it at the forge (it was just tested or
    chosen)."""
    ref = git_connections.root_ref(root)
    assert ref is not None  # narrowing: the route validated `root`
    try:
        credential = git_access.resolve_for_user(session, user.id, ref, retry="then reload this page")
        entry = git_connections.ConnectionState(root, "connected", credential, None)
    except git_access.GitAccessNeedsReconnect as exc:
        entry = git_connections.ConnectionState(
            root, "needs_reconnect", git_connections._selected(session, user, ref), str(exc)
        )
    except git_access.GitAccessError as exc:
        entry = git_connections.ConnectionState(root, "not_connected", None, str(exc))
    return _out(entry, user)


# ── The Repo URL lookup: the repositories the caller's own connections reach (`app/forge_repos.py`) ──────

class GitRepoOut(BaseModel):
    url: str
    forge: str
    root: str
    owner: str
    name: str
    # Set only for a self-managed GitLab under a sub-path: the project form's "GitLab base URL".
    base_url: Optional[str] = None


class GitRepoSourceOut(BaseModel):
    """One connected root's read. `failed` (with `detail`) or `truncated` means its list is incomplete, so a
    URL missing from it says nothing; an `ok` one's `detail` names a limit of its read."""

    root: str
    forge: str
    forge_label: str
    state: Literal["ok", "failed"]
    detail: Optional[str] = None
    truncated: bool = False


class GitReposOut(BaseModel):
    repos: list[GitRepoOut]
    sources: list[GitRepoSourceOut]


@router.get("/api/git-repos", response_model=GitReposOut)
@feature("credentials", "view", own_credential=True)
def list_git_repos(
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitReposOut:
    """Every repository the signed-in person's own git connections reach, across every connected forge
    root and every org/workspace in it. Takes no user id: like Git Access it is self-service only. DB reads
    first, then the transaction is released before the forge reads (`app/db.py`)."""
    plans, failed = forge_repos.plan_reads(session, user)
    session.rollback()
    sources = failed + forge_repos.list_repos(plans)
    return GitReposOut(
        repos=[GitRepoOut(**vars(repo)) for source in sources for repo in source.repos],
        sources=[_source_out(s) for s in sources],
    )


class GitRepoOwnerOut(BaseModel):
    """One owner a new repository can be created in: `owner` on the connected `root`, as `GitRepoOut.owner` spells
    it (`POST /api/git-repos` takes the pair)."""

    root: str
    forge: str
    owner: str
    # The person's own account on that root (GitHub login, GitLab username), not an org, group or workspace.
    own: bool = False
    # Why the connection cannot create here (`forge_repos.Source.blocked`): listed, but the picker does not offer it.
    blocked: Optional[str] = None
    # The fix for a GitHub App not installed on this owner: the designated App's install page, where an owner installs
    # it and anyone else requests it. None for every other owner, or when the App has no usable slug.
    install_url: Optional[str] = None
    # The owner's numeric GitHub account id, with `install_url`: the console passes it as `suggested_target_id`, so the
    # install page opens on this owner rather than GitHub's account picker. None when GitHub did not answer one.
    account_id: Optional[int] = None
    # Set when Jaah's GitHub App may not create here (`forge_repos.Source.handoff`): github.com's new-repository page
    # for this owner, where the person creates it with their own rights; Create then only initializes it.
    create_url: Optional[str] = None


class GitRepoOwnersOut(BaseModel):
    owners: list[GitRepoOwnerOut]
    sources: list[GitRepoSourceOut]
    # Whether an OAuth App is designated (`git_access.app_config`), read only when an owner has a `create_url`: the
    # create wizard then connects through it in a popup, whose token creates with the person's own rights, instead
    # of handing the person to github.com.
    oauth_connect: bool = False


@router.get("/api/git-repos/owners", response_model=GitRepoOwnersOut)
@feature("credentials", "view", own_credential=True)
def list_git_repo_owners(
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitRepoOwnersOut:
    """Every owner the signed-in person's own git connections can create a repository in — their own account and
    every org, group, workspace or Azure project they may create in, with or without repositories
    (`forge_repos.list_owners`). Self-service only, read as `GET /api/git-repos` is."""
    plans, failed = forge_repos.plan_reads(session, user)
    session.rollback()
    sources = failed + forge_repos.list_owners(plans)
    not_installed = any(forge_repos.GITHUB_APP_NOT_INSTALLED in s.blocked.values() for s in sources)
    install = _app_install_url() if not_installed else None
    owners = [GitRepoOwnerOut(root=s.root, forge=s.forge, owner=owner, own=owner == s.account,
                              blocked=s.blocked.get(owner),
                              install_url=install if s.blocked.get(owner) == forge_repos.GITHUB_APP_NOT_INSTALLED else None,
                              account_id=s.owner_ids.get(owner)
                              if install and s.blocked.get(owner) == forge_repos.GITHUB_APP_NOT_INSTALLED else None,
                              create_url=_github_new_url(owner)
                              if s.root == "github.com" and owner in s.handoff and owner not in s.blocked else None)
              for s in sources for owner in s.owners]
    return GitRepoOwnersOut(
        owners=owners,
        sources=[_source_out(s) for s in sources],
        oauth_connect=any(o.create_url for o in owners) and _oauth_app_designated(),
    )


def _oauth_app_designated() -> bool:
    try:
        git_access.app_config(kind="oauth_app", open_client_secret=False)
    except git_access.GitAccessError:
        return False
    return True


def _github_new_url(owner: str) -> str:
    return f"https://github.com/new?{urllib.parse.urlencode({'owner': owner})}"


def _app_install_url() -> Optional[str]:
    """The designated GitHub App's install page (`git_connect.install_url`), or None when no App is designated."""
    try:
        app = git_access.app_config(kind="github_app", open_client_secret=False)
    except git_access.GitAccessError:
        return None
    return install_url(app.slug)


def _source_out(s: forge_repos.Source) -> GitRepoSourceOut:
    return GitRepoSourceOut(root=s.root, forge=s.forge, forge_label=FORGE_LABELS[s.forge], state=s.state,
                            detail=s.detail, truncated=s.truncated)


class GitDefaultBranchOut(BaseModel):
    default_branch: Optional[str] = None
    # No branch at all (zero commits): the create wizard initializes it. None when it cannot be read.
    empty: Optional[bool] = None


@router.get("/api/git-repos/default-branch", response_model=GitDefaultBranchOut)
@feature("credentials", "view", own_credential=True)
def git_repo_default_branch(
    url: str,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitDefaultBranchOut:
    """`url`'s default branch and whether it has no branch at all, read through the signed-in person's own
    connection for its root; null for a URL that does not parse, a root they have not connected, or a forge read
    that fails — the form keeps its seed then."""
    ref = _parsed_repo_url(url)
    if ref is None:
        return GitDefaultBranchOut()
    plan = forge_repos.plan_read(session, user, ref)
    session.rollback()
    if plan is None:
        return GitDefaultBranchOut()
    return GitDefaultBranchOut(default_branch=forge_repos.default_branch(plan, ref),
                               empty=forge_repo_init.is_empty(plan, ref))


def _parsed_repo_url(url: str) -> Optional[forges.RepoRef]:
    """`url` as a repository ref, or None. An unknown host is read as a self-managed GitLab, as the project form
    declares it."""
    try:
        return forges.parse_repo_url(url, None)
    except forges.ForgeUrlError:
        try:
            return forges.parse_repo_url(url, "gitlab")
        except forges.ForgeUrlError:
            return None


def _not_connected(forge: str) -> HTTPException:
    return HTTPException(
        status_code=422, detail=f"Connect {FORGE_LABELS[forge]} in Profile > Git first: Jaah writes through your own connection.")


class GitRepoInitializeIn(BaseModel):
    url: str
    # The README's title.
    name: str = Field(default="", max_length=200)


class GitRepoInitializeOut(BaseModel):
    # False: the repository already had a branch, and nothing was written.
    initialized: bool


@router.post("/api/git-repos/initialize", response_model=GitRepoInitializeOut)
@feature("projects", "create")
def initialize_git_repo(
    body: GitRepoInitializeIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitRepoInitializeOut:
    """Give an EMPTY repository (no branch, zero commits) `main` — README.md and .gitignore — and `develop`, through
    the signed-in person's own connection; a repository with any branch is left alone. The create wizard calls it
    on Create, before the project's POST."""
    ref = _parsed_repo_url(body.url)
    if ref is None:
        raise HTTPException(status_code=422, detail="This is not a repository URL Jaah can use.")
    plan = forge_repos.plan_read(session, user, ref)
    session.rollback()
    if plan is None:
        raise _not_connected(ref.forge)
    try:
        return GitRepoInitializeOut(initialized=forge_repo_init.initialize(plan, ref, body.name))
    except forge_repo_init.GitHubAppNotInstalled as exc:
        linked = forge_repo_init.GitHubAppNotInstalled(exc.owner, _app_install_url())
        raise HTTPException(status_code=422, detail=f"Could not initialize {ref.path}: {linked}") from exc
    except forge_repo_init.ForgeWriteError as exc:
        raise HTTPException(status_code=422, detail=f"Could not initialize {ref.path}: {exc}") from exc


class GitRepoCreateIn(BaseModel):
    # A connected root (`forges.RepoRef.root`), the owner the Repo URL list groups by, and the new name.
    root: str
    owner: str
    name: str


class GitRepoCreatedOut(GitRepoOut):
    # Set when the repository was created but not initialized: it exists, so the wizard keeps its URL and
    # `POST /api/git-repos/initialize` finishes it (`forge_repo_init.initialize` resumes a cut-short one).
    init_error: Optional[str] = None


@router.post("/api/git-repos", response_model=GitRepoCreatedOut, status_code=201)
@feature("projects", "create")
def create_git_repo(
    body: GitRepoCreateIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GitRepoCreatedOut:
    """Create the private repository `owner/name` on `root` through the signed-in person's own connection, then
    give it `main` and `develop` (`forge_repo_init`). The create wizard's "Create a new repository", on Create.
    A refused create is a 422; a created repository whose initialization failed is still a 201, naming why."""
    root_ref = git_connections.root_ref(body.root)
    if root_ref is None:
        raise HTTPException(status_code=422, detail="Pick an owner from the list.")
    plan = forge_repos.plan_read(session, user, root_ref)
    session.rollback()
    if plan is None:
        raise _not_connected(root_ref.forge)
    owner, name = body.owner.strip(), body.name.strip()
    init_error = None
    try:
        ref = forge_repo_init.create(plan, owner, name)
    except forge_repo_init.CreatedUnreachable as exc:
        # Made, but the App cannot reach it yet: the retry's initialize runs once it can.
        ref, init_error = exc.ref, str(exc)
    except forge_repo_init.ForgeWriteError as exc:
        raise HTTPException(status_code=422, detail=f"Could not create {owner}/{name}: {exc}") from exc
    if init_error is None:
        try:
            forge_repo_init.initialize(plan, ref, name)
        except forge_repo_init.ForgeWriteError as exc:
            init_error = f"Created {ref.path}, but could not initialize it: {exc}"
    parent, _, repo_name = ref.path.rpartition("/")
    return GitRepoCreatedOut(url=ref.web_url(), forge=ref.forge, root=ref.root, owner=parent, name=repo_name,
                             base_url=f"https://{plan.root}" if "/" in plan.root else None, init_error=init_error)
