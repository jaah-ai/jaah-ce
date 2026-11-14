"""Secrets and metered resources a task's runner fetches: git tokens, credentials, API keys, Bedrock and usage."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import (
    account_auth,
    carryover,
    console_key_leases,
    credential_crypto,
    credential_keys,
    data_access_events,
    edition_features,
    forges,
    git_access,
    git_reach,
    jaah_repos,
    node_credentials,
    nodevault_grants,
    oauth2_tokens,
    oauth_providers,
    slot_allocation,
    slot_state,
    task_ancestry,
)
# `principal_for_user`: the sanctioned way to build a `Principal` outside a request; the task-credential
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import infra
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import has_project_key, task_roster_select
from app.credential_crypto import has_sealed_secret, open_secret
from app.credentials import health as credential_health
from app.credentials.oauth_wizards import connect_for_type
from app.credentials.registry import field_keys, type_label
from app.db_async import DbDep, on_session, on_thread, run_blocking_in_session
from app.tenancy import seam
from app.task_ancestry import launcher_user_id
from app.models.credentials import Account, Credential, NV_GRANT_DELIVERED, NodeVaultGrant
from app.models.identity import AdminUser
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.entity_shape import StrictModel, names_of, or_404, from_row
from app.routes.agent import auth
from app.routes.agent.auth import (
    AgentDoor,
    _check_token,
    _check_token_for_task,
    _task_has_live_vm_on,
    _task_holder_node_ids,
    _wire_project,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])
# The header a node sends a trigger preflight's grant in (`apps/jaahd/cred_broker.py::_fetch_trigger_git_login`).
TRIGGER_GRANT_HEADER = "X-Trigger-Grant"
log = logging.getLogger("agent-routes")


# ── Git access: hand a node the credential the WORK's owner connected ─────────
# Replaces the fleet-wide PAT broker (prohibited under tenancy: GitHub access is the credential of whoever
# started the work). These resolve a person's credential, so the org boundary follows
# the identity; which person is `app/git_access.py`'s rule, with no shared fallback. A token, not the row:
# askpass wants username/password, and `git_access.token_for` refreshes under a row lock.

def _require_derived_node(caller: node_credentials.AgentCaller) -> None:
    """Refuse a caller whose credential does not name a node. Secret-serving routes only.

    Used by the two git-token routes and `get_account_api_key`, each returning credential material stronger
    than anything else on this surface. Non-deriving arms (CP loopback, dev bypass) get tasks unchecked
    from `_task_or_404_for_node`, so one leaked non-deriving token could enumerate every task's GitHub
    token — worse than the retired fleet-wide PAT. No legitimate caller is lost: askpass comes through a
    per-node daemon, the CP has no GitHub client, and a local stack runs no runner. 403, not
    404: this credential may never ask.
    """
    if not caller.is_derived:
        log.warning("git access: refused a caller whose credential names no node (arm=%s)", caller.arm)
        raise HTTPException(
            status_code=403,
            detail="a git token requires a per-node credential; this caller presented a shared token",
        )
    _refuse_a_carried_over_caller(caller)


def _refuse_a_carried_over_caller(caller: node_credentials.AgentCaller) -> None:
    """403 for a carried-over request (`app/carryover.py`) on a route serving credential material. The runner
    already holds what it launched with; its own task's git token is the one exception, which
    `get_task_git_token` grants without calling this (D18)."""
    if carryover.request_task() is None:
        return
    log.warning("secret route: refused node=%s carried over for task %s", caller.node_id, carryover.request_task())
    raise HTTPException(
        status_code=403,
        detail="this task moved to another org; a carried-over runner may fetch no secret but its git token",
    )


def _git_task_or_404(
    session: Session, caller: node_credentials.AgentCaller, task_id: int
) -> Task:
    """`_task_or_404_for_node`, with its no-holder arm closed. Git-token routes only.

    A task with no worktree has no runner to authenticate; otherwise every draft, settled row and epic would
    be readable from any node, bounded only by whether its owner connected GitHub.
    """
    task = auth._task_or_404_for_node(session, caller, task_id)
    holder_node_ids = _task_holder_node_ids(session, task_id)
    if not holder_node_ids and not _task_has_live_vm_on(session, task_id, caller.node_id):
        log.warning("git access: refused task=%s to node=%s (task holds no worktree)", task_id, caller.node_id)
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return task


class GitTokenOut(BaseModel):
    """One resolved git login and the repository it is for — the pinned wire.

    `username` and `token` keep their pre-forge meaning, so an older node's askpass keeps working on
    GitHub. `scheme` is `basic` (username + token) or `bearer` (the token alone, as an
    `Authorization: Bearer` header); the username table is `git_access.git_login_for`'s. The rest
    describes the repo (`forges.RepoRef`): `host` and `root` say where the login may be sent: the node's
    git credential helper (`apps/jaahd/git_credential_helper.py`) refuses to answer git for any other
    host, or a path outside a sub-path root (C5). `api_username` is additive: the REST API's Basic
    username where it differs from git's (`git_access._REST_USERNAME_FIELDS`), else `""`; git never
    reads it, so an older node keeps working for git. `jaah_repo_prefix` is additive: `<github org>/<org slug>-` for the
    asking project's org (`jaah_repos.jaah_repo_prefix`), else `""`; the node's credential helper refuses a destination
    in the Jaah org whose path does not start with it. Never log `token`.
    """

    scheme: str
    username: str
    token: str
    host: str
    root: str
    forge: str
    api_base: str
    repo_path: str
    web_url: str
    api_username: str = ""
    # Additive: set when GitHub says no credential of the person's can write to the repository
    # (`git_reach.writer_for_task`) — what is missing and where to fix it, so `jaah-forge` names it on a refused
    # write. `""` when the served login can write, or it cannot be told.
    write_problem: str = ""
    jaah_repo_prefix: str = ""


def _prefix_of(session: Session, org_id: UUID) -> str:
    """`GitTokenOut.jaah_repo_prefix` for org `org_id`."""
    return jaah_repos.jaah_repo_prefix(jaah_repos.org_slug_of(session, org_id))


def _git_token_or_refusal(
    session: Session, resolve, *, repo_name: str = "", jaah_repo_prefix: str = "",
) -> GitTokenOut:
    """Run one `git_access` resolution — `resolve()` returns `(credential, repo)`, or `(credential, repo,
    write_problem)` from `git_reach` — and turn its failure into an HTTP answer.

    409 for every `GitAccessError`: a conflict with configuration (no connected account, deleted
    credential, unconfigured App, a repo URL naming no forge), not an authorization failure. `repo_name` (a
    connected repository asked for by id) leads the refusal, so a runner's blocker can name it.
    """
    lead = f"Repository {repo_name}: " if repo_name else ""
    try:
        credential, repo, *problem = resolve()
        login = git_access.git_login_for(session, credential, repo)
        credential_id = credential.id
    except git_access.GitAccessNeedsReconnect as exc:
        # 410 for a dead authorization (a refused renewal, or the forge reported the token revoked): the
        # credential is configured but the grant is gone, and only its owner can reconnect (the detail
        # names the forge and where).
        log.warning("git access: refused (connection expired, needs re-authorization)")
        raise HTTPException(status_code=410, detail=f"{lead}{exc}") from exc
    except git_access.GitAccessError as exc:
        # `str(exc)` is safe by the exception's contract (names a person, setting or credential, never a token),
        # so the detail reaches the runner's terminal.
        log.warning("git access: refused (%s)", type(exc).__name__)
        raise HTTPException(status_code=409, detail=f"{lead}{exc}") from exc
    out = GitTokenOut(
        scheme=login.scheme, username=login.username, token=login.token, api_username=login.api_username,
        host=repo.host, root=repo.root, forge=repo.forge, api_base=repo.api_base(),
        repo_path=repo.path, web_url=repo.web_url(), write_problem=problem[0] if problem else "",
        jaah_repo_prefix=jaah_repo_prefix,
    )
    # Best-effort, after the answer is built (`credential_health.record_use`): never fails or alters it.
    credential_health.record_use(session, [credential_id])
    return out


def _jaah_repo_token_or_none(session: Session, project: Optional[Project], repo_of, *,
                             node_id: Optional[UUID]) -> Optional[GitTokenOut]:
    """A Jaah-made repository's runner login (`easy_mode_runner_tokens`): a one-repo installation token of the
    console's GitHub App when `repo_of()` — any of the project's repositories — is the project's own `jaah_repos` row
    in a served state (`jaah_repos.runner_app_repo`), else None and the caller resolves a person exactly as before. Write
    while the row is `active`, read-only while the project moves off it (`jaah_repos.permissions_for`).

    Node rule: an Easy Mode project's login is served only on its org's Easy Mode shared node (`easy_mode.shared_node`;
    none, or any other node, is refused), so a tenant's own machine never holds a token on Jaah's GitHub org for a
    project Jaah hosts. Any other Jaah-made project runs on its org's own machines: every node the route already
    admits is served its one-repo login. The App's key never leaves the control plane. Refusals are 409 (they name
    what to fix). Every caller runs `_require_org_prefix` before this, in every edition."""
    if not edition_features.enabled("customers"):
        return None
    from app import easy_mode  # cloud edition only (`customers`), checked above

    if project is None:
        return None
    try:
        repo = repo_of()
        row = jaah_repos.runner_app_repo(session, project, repo)
        if row is None:
            return None
        if project.easy_mode:
            shared = easy_mode.shared_node(session, project.org_id)
            if node_id is None or shared is None or node_id != shared.id:
                raise git_access.GitAccessError(
                    "This Easy Mode project's git login is served only on its organization's shared node.")
        login = jaah_repos.login_for(row, repo)
    except git_access.GitAccessError as exc:
        log.warning("git access: refused jaah repo runner login project=%s (%s)", project.id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GitTokenOut(
        scheme=login.scheme, username=login.username, token=login.token, host=repo.host, root=repo.root,
        forge=repo.forge, api_base=repo.api_base(), repo_path=repo.path, web_url=repo.web_url(),
        jaah_repo_prefix=_prefix_of(session, project.org_id),
    )


def _require_org_prefix(session: Session, org_id: UUID, repo_of, *, repo_name: str = "") -> None:
    """409 when `repo_of()` is in the Jaah org without org `org_id`'s prefix (`jaah_repos.require_org_prefix`) — run
    by every git-login route before any login, the App's or a person's, is resolved. A `repo_of()` that itself fails is
    left to the login path, which refuses it with its own reason."""
    try:
        repo = repo_of()
    except git_access.GitAccessError:
        return
    try:
        jaah_repos.require_org_prefix(session, org_id, repo)
    except git_access.GitAccessError as exc:
        log.warning("git access: refused %s (not this org's Jaah repo)", repo.path)
        raise HTTPException(status_code=409, detail=f"Repository {repo_name}: {exc}" if repo_name else str(exc)) from exc


def _connected_repo_or_404(session: Session, project_id: Optional[int], repo_id: int, *, subject: str) -> ProjectRepo:
    """Repository `repo_id` of project `project_id`, or 404 — the same 404 whether that row does
    not exist or belongs to another project, so no id outside the project is confirmed. `subject` names what
    was asked for in the refusal."""
    row = None
    if project_id is not None:
        row = session.scalars(
            select(ProjectRepo).where(ProjectRepo.id == repo_id, ProjectRepo.project_id == project_id)
        ).first()
    if row is None:
        log.warning("git access: refused repo_id=%s (not a repository of project %s)", repo_id, project_id)
        raise HTTPException(status_code=404, detail=f"repository {repo_id} is not connected to {subject}")
    return row


@router.get("/tasks/{task_id}/git-token", response_model=GitTokenOut)
@infra(Tenancy.ORG_SCOPED)
async def get_task_git_token(
    task_id: int,
    db: DbDep,
    repo_id: Optional[int] = None,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> GitTokenOut:
    """The git credential for one task — the credential of whoever started it, or its parent epic.

    Two checks: `_require_derived_node` (the credential names a node) and `_git_task_or_404` (this node
    holds the task). The daemon's peer-derived `task_id` is the first wall; this is the second.

    `repo_id` picks one of the project's connected repositories — the daemon reads it off the
    runner's checkout (`cred_broker._caller_repo_id`); absent, the primary. A repository the task's project
    does not connect is 404, and the login is the same person's connection for THAT repository's forge root.

    A carried-over runner (`app/carryover.py`) is the one secret this route serves across a project move, for
    its own task only (D18): `_check_token_for_task` has already refused any other task, and the token is
    minted exactly as at launch — the same person, read in the org their git connection lives in.
    """
    # Both halves interleave DB work with forge HTTP (the reach check, a token refresh, the revocation probe): each
    # runs in a worker thread on a session of its own (`app/db_async.py::run_blocking_in_session`).
    out = await run_blocking_in_session(
        _get_task_git_token,
        task_id=task_id,
        repo_id=repo_id,
        caller=caller,
        route=db,
    )
    if isinstance(out, _CarriedSource):
        # A carried-over runner's login is read in the org it came FROM, on a session of its own (one session belongs
        # to one org).
        out = await run_blocking_in_session(
            _carried_over_git_token,
            out,
            task_id=task_id,
            node_id=caller.node_id,
            route=db,
        )
    # Task and node, never the token or config: answers "which task borrowed whose access".
    log.info("git access: served task=%s node=%s repo=%s%s", task_id, caller.node_id,
             repo_id if repo_id is not None else "primary",
             " (carried over)" if carryover.request_task() is not None else "")
    return out


def _get_task_git_token(
    session: Session,
    task_id: int,
    repo_id: Optional[int],
    caller: node_credentials.AgentCaller,
) -> "GitTokenOut | _CarriedSource":
    carried = carryover.request_task() is not None
    if not carried:
        _require_derived_node(caller)
    task = _git_task_or_404(session, caller, task_id)
    if not slot_allocation.task_has_repo(session, task):
        # A folder task has no repository to reach: a project's stored-but-unused `repo_url` must not
        # buy it the starter's token, and a clone it made itself would carry no access guard.
        log.warning("git access: refused task=%s node=%s (folder slot, no repository)", task_id, caller.node_id)
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    row = (_connected_repo_or_404(session, task.project_id, repo_id, subject=f"task {task_id}'s project")
           if repo_id is not None else None)
    if row is not None and row.is_primary:
        row = None  # the primary asked for by id answers exactly as without one

    def _repo() -> forges.RepoRef:
        return git_access.project_repo_ref(row) if row is not None else git_access.task_repo(session, task)

    repo_name = row.name if row else ""
    _require_org_prefix(session, task.org_id, _repo, repo_name=repo_name)
    if carried:
        return _carried_source(session, caller, task, _repo, repo_name=repo_name)
    else:
        easy = _jaah_repo_token_or_none(
            session, session.scalar(select(Project).where(Project.id == task.project_id)), _repo,
            node_id=caller.node_id)
        if easy is not None:
            return easy

        def _resolve() -> tuple[Credential, forges.RepoRef, str]:
            repo = _repo()
            # The starter's credential that can write to it (`git_reach`), else their selected one and why not.
            pick = git_reach.writer_for_task(session, task, repo)
            problem = f"{pick.problem} {pick.fix_url}".strip() if pick.fix_url else pick.problem
            return pick.credential, repo, problem

        return _git_token_or_refusal(session, _resolve, repo_name=repo_name,
                                     jaah_repo_prefix=_prefix_of(session, task.org_id))


class ForgeAuthIn(StrictModel):
    """The forge's verdict on the login `get_task_git_token` served (`git_credential_helper.report_forge_auth`)."""

    # git's `host` attribute: a hostname with an optional port, as `forges.RepoRef.host` spells it.
    host: str = Field(max_length=255, pattern=r"^[A-Za-z0-9.\-]+(:[0-9]{1,5})?$")
    repo_path: Optional[str] = Field(default=None, max_length=1024)


class ForgeAuthOut(BaseModel):
    # Open refusals in this attempt; `terminal`: this report ended the lane run (`lane_forge_auth`).
    count: int
    terminal: bool


def _forge_auth_report(session: Session, caller: node_credentials.AgentCaller, task_id: int, body: ForgeAuthIn,
                       *, rejected: bool) -> ForgeAuthOut:
    """Both forge-auth routes: the git-token route's caller binding, then `lane_forge_auth.report`."""
    _require_derived_node(caller)
    task = _git_task_or_404(session, caller, task_id)
    # Only a merge-lane run's verdicts count, and an edition without the lane runs none: nothing to record.
    out: dict[str, Any] = {"count": 0, "terminal": False}
    if edition_features.enabled("merge_lane"):
        from app import lane_forge_auth

        out = lane_forge_auth.report(session, task, rejected=rejected, host=body.host, repo_path=body.repo_path)
    log.info("git access: forge %s task=%s node=%s host=%s", "rejected" if rejected else "accepted", task_id,
             caller.node_id, body.host)
    return ForgeAuthOut(**out)


@router.post("/tasks/{task_id}/forge-auth-rejected", response_model=ForgeAuthOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def post_forge_auth_rejected(
    session: Session,
    task_id: int,
    body: ForgeAuthIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> ForgeAuthOut:
    """The forge refused the task's git login after the node's fresh read. A merge-lane run's second refusal
    in one attempt ends it `system_failed` naming the credential, with one alert (`lane_forge_auth`)."""
    # Interleaves DB work with blocking I/O (a run-ending refusal raises an alert, whose mail and chat legs open their own sessions): a worker thread on its own session (`app/db_async.py::run_blocking_in_session`).
    return _forge_auth_report(session, caller, task_id, body, rejected=True)


@router.post("/tasks/{task_id}/forge-auth-accepted", response_model=ForgeAuthOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def post_forge_auth_accepted(
    session: Session,
    task_id: int,
    body: ForgeAuthIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> ForgeAuthOut:
    """The forge took the login after a refusal: the retry in place worked, and the refusal is closed."""
    return _forge_auth_report(session, caller, task_id, body, rejected=False)


@dataclass(frozen=True)
class _CarriedSource:
    """What a carried-over runner's git login is read from: the org the carry-over came from, the task's
    started-by chain and its repo, all read in the request's (new) org."""

    from_org: UUID
    chain: list[UUID]
    repo: forges.RepoRef
    repo_name: str
    jaah_repo_prefix: str = ""


def _carried_source(
    session: Session, caller: node_credentials.AgentCaller, task: Task, task_repo, *, repo_name: str = "",
) -> _CarriedSource:
    """`get_task_git_token` for a carried-over runner, its first half. The repo (`task_repo()`, the asked-for one of
    the task's repositories) and the person are the task's, read in the request's (new) org; the person's git
    connection is read in the org the carry-over came FROM by `_carried_over_git_token`, on a session of its own,
    since one session belongs to one org (`seam.check_session_stamp`)."""
    # Closed between the bind and here: the node is back to today's answer.
    from_org = or_404(
        carryover.from_org_for(session, node_id=caller.node_id, task_id=task.id),
        detail=f"task {task.id} not found",
    )
    try:
        repo = task_repo()
    except git_access.GitAccessError as exc:
        raise HTTPException(status_code=409, detail=f"Repository {repo_name}: {exc}" if repo_name else str(exc)) from exc
    chain = task_ancestry.started_by_chain(session, task.id)
    return _CarriedSource(from_org=from_org, chain=chain, repo=repo, repo_name=repo_name,
                          jaah_repo_prefix=_prefix_of(session, task.org_id))


def _carried_over_git_token(source: Session, carried: _CarriedSource, *, task_id: int, node_id: Any) -> GitTokenOut:
    """`_carried_source`'s second half, on its own session (`db_async.run_in_session`) in the source org."""
    repo, repo_name = carried.repo, carried.repo_name
    with seam.detached(), seam.bound_to(carried.from_org):
        person_id = task_ancestry.first_human_starter(source, carried.chain)
        if person_id is not None:
            out = _git_token_or_refusal(
                source, lambda: (git_access.resolve_for_user(source, person_id, repo), repo), repo_name=repo_name,
                jaah_repo_prefix=carried.jaah_repo_prefix)
            source.commit()
            return out
    # No starter of the source org: the task's new org must not lend its own people's access to this node.
    log.warning("git access: refused carried task=%s node=%s (no starter in source org %s)", task_id,
                node_id, carried.from_org)
    raise HTTPException(status_code=404, detail=f"task {task_id} not found")


@router.get("/git-token", response_model=GitTokenOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def get_clone_git_token(
    session: Session,
    project: str = "",
    project_id: Optional[int] = None,
    repo_id: Optional[int] = None,
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> GitTokenOut:
    """The git credential for a pool clone — work that belongs to no task, on behalf of the operator who
    declared that project's slots on this node.

    The caller is the node daemon materializing a slot, so the person is the slot's declarer
    (`git_access.resolve_for_worktree_clone`). `project` selects, it does not authorize (the node credential
    plus the declarer's grant, re-checked in `resolve_for_user`); without it a clone could use access given
    for another project. The daemon supplies it from its own clone context (`apps/jaahd/clone_identity.py`),
    not the askpass request: `project_id` (`project`, the display name, never resolves — `_wire_project`).
    Requires a derived caller, as the task route does. `repo_id` is one of that project's
    connected repositories, from the roster's `repos[].id` the daemon declared the clone with;
    absent, the primary; a repository the project does not connect is 404.
    """
    # Interleaves DB work with blocking I/O (a token refresh and the revocation probe): a worker thread on its own session (`app/db_async.py::run_blocking_in_session`).
    _require_derived_node(caller)
    if project_id is None and not project.strip():
        # Defaulted and refused here rather than declared required, so the refusal names a fix instead of
        # FastAPI's schema error.
        raise HTTPException(
            status_code=409,
            detail=(
                "this git credential is resolved per project, and the caller named none. A pool clone "
                "declares the project it is cloning from the node daemon's own clone context "
                "(`apps/jaahd/clone_identity.py`); a request arriving without one is a git operation "
                "the daemon ran outside that context, and it has no declarer to resolve"
            ),
        )
    project_row = _wire_project(session, project_id)
    if project_row is None:
        # 404 on the project: nothing an operator could connect would fix it.
        named = project_id if project_id is not None else repr(project)
        raise HTTPException(status_code=404, detail=f"unknown project {named}")
    row = (_connected_repo_or_404(session, project_row.id, repo_id, subject=f"project {project_row.id}")
           if repo_id is not None else None)
    if row is not None and row.is_primary:
        row = None  # the primary asked for by id answers exactly as without one
    credential_id: Optional[UUID] = None

    def _repo() -> forges.RepoRef:
        return (git_access.project_repo_ref(row, retry=git_access.CLONE_RETRY) if row is not None
                else git_access.project_repo(project_row, retry="then materialise the slot again"))

    _require_org_prefix(session, project_row.org_id, _repo, repo_name=row.name if row else "")
    # Only for a node that declared this project's slots: the person resolution below refuses any other, and a
    # Jaah repo's clone must not skip that check.
    easy = (_jaah_repo_token_or_none(session, project_row, _repo, node_id=caller.node_id)
            if caller.node_id is not None
            and git_access.clone_owner_id(session, caller.node_id, project_row.id) is not None else None)
    if easy is not None:
        log.info("git access: served clone jaah repo runner login node=%s project=%s repo=%s",
                 caller.node_id, project_row.id, repo_id if repo_id is not None else "primary")
        return easy

    def _resolve() -> tuple[Credential, forges.RepoRef]:
        nonlocal credential_id
        repo = _repo()
        credential = git_access.resolve_for_worktree_clone(session, caller.node_id, project_row.id, repo)
        # Read now: a failed use stamp's rollback expires the row (`credential_health._stamp`).
        credential_id = credential.id
        return credential, repo

    out = _git_token_or_refusal(session, _resolve, repo_name=row.name if row else "",
                                jaah_repo_prefix=_prefix_of(session, project_row.org_id))
    # The credential id (never token or config), so the journal says whose access was borrowed.
    log.info(
        "git access: served clone credential node=%s project=%s repo=%s credential=%s",
        caller.node_id, project_row.id, repo_id if repo_id is not None else "primary", credential_id)
    return out


@router.get("/trigger-preflights/git-token", response_model=GitTokenOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def get_trigger_preflight_git_token(
    session: Session,
    grant: str = Header(default="", alias=TRIGGER_GRANT_HEADER, max_length=2048),
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> GitTokenOut:
    """The git credential for a "Repo script" trigger's throwaway clone: the trigger OWNER'S login for the
    project's primary repository (D14). The only credential path a preflight has.

    The grant (`X-Trigger-Grant`, a header so no URL or access log ever carries it) is the blob
    `trigger_preflight_script.mint_grant` put in the node's dispatch; it authorizes, nothing else does. Refused 403
    unless its MAC is ours, it has not expired and it names THIS node; then 404 unless the trigger still exists in
    the caller's org, is a script preflight of the grant's project, is neither paused nor failed, the node is still
    assigned that project, and the owner still holds `tasks:create` on it — each re-asked now, never read from the
    grant. A replay by the same node inside the grant's expiry is the accepted residual (R-7).
    """
    # Deferred: trigger_intake pulls the route layer.
    from app import automation_fire, trigger_intake, trigger_preflight_script  # noqa: PLC0415
    from app.models.automations import TaskTrigger  # noqa: PLC0415
    from app.triggers import PREFLIGHT_SCRIPT  # noqa: PLC0415

    _require_derived_node(caller)
    claims = trigger_preflight_script.verify_grant(grant, node_id=caller.node_id)
    if claims is None:
        log.warning("git access: refused trigger preflight node=%s (grant invalid, expired or another node's)",
                    caller.node_id)
        raise HTTPException(status_code=403, detail="the trigger preflight grant is invalid, expired or not this node's")
    trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == claims.trigger_id))
    owner: Optional[AdminUser] = None
    project_row: Optional[Project] = None
    if trigger is None:
        refusal = "trigger gone"
    elif trigger.project_id != claims.project_id or trigger.preflight_kind != PREFLIGHT_SCRIPT:
        refusal = "trigger's project or preflight changed"
    elif trigger.paused_at is not None or trigger.failed_at is not None:
        refusal = "trigger paused or failed"
    elif caller.node_id not in slot_allocation.hosting_node_ids(session, claims.project_id):
        refusal = "node no longer runs the project"
    else:
        owner = trigger_intake.trigger_owner(session, trigger)
        project_row = session.scalar(select(Project).where(Project.id == claims.project_id))
        refusal = ("owner lacks tasks:create" if owner is None
                   or automation_fire.owner_outside_org(owner.org_id, trigger.org_id)
                   or not has_project_key(principal_for_user(session, owner), permission_key("tasks", "create"),
                                          claims.project_id)
                   else "project gone" if project_row is None else "")
    if refusal or owner is None or project_row is None:
        log.warning("git access: refused trigger preflight trigger=%s node=%s (%s)", claims.trigger_id,
                    caller.node_id, refusal)
        raise HTTPException(status_code=404, detail="trigger not found")
    owner_id, project = owner.id, project_row
    retry = "then fire the trigger again"

    def _resolve() -> tuple[Credential, forges.RepoRef]:
        repo = git_access.project_repo(project, retry=retry)
        return git_access.resolve_for_user(session, owner_id, repo, retry=retry), repo

    _require_org_prefix(session, project.org_id, lambda: git_access.project_repo(project, retry=retry))
    out = _git_token_or_refusal(session, _resolve, jaah_repo_prefix=_prefix_of(session, project.org_id))
    # Trigger, node and owner — never the token.
    log.info("git access: served trigger preflight trigger=%s node=%s owner=%s project=%s", claims.trigger_id,
             caller.node_id, owner_id, claims.project_id)
    return out


# ── Account API keys: the ONE key an API-key runner account authenticates with ─────────────────────────
# Shaped like the git-token routes, not the deleted broker: it takes the account, never a name to resolve.
# The WHERE clause is the authorization: `slug`, `enabled` (a disabled account's key is not spendable),
# `auth_kind` (Bedrock/Vertex cannot inherit this door) and the credential pointer; no composable name
# reaches another credential. 404 for every refusal with one detail, so slugs cannot be enumerated.

class AccountApiKeyOut(BaseModel):
    """One API-key account's key, for the runner launching on it. NEVER LOG `api_key`."""

    api_key: str
    # The Anthropic workspace an unscoped key must name on every request (`account_auth.WORKSPACE_ID_CONFIG_KEY`,
    # non-secret, stored beside the key); `""` when none. The Claude adapter turns it into a header.
    workspace_id: str = ""


@router.get("/accounts/{slug}/api-key", response_model=AccountApiKeyOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def get_account_api_key(
    session: Session,
    slug: str,
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> AccountApiKeyOut:
    """The API key one enabled API-key account authenticates with (Anthropic or DeepSeek).

    `_require_derived_node`: these are live billable keys with no expiry, and a non-deriving caller could
    otherwise walk every slug. The slug is validated before any query by
    `account_auth.slug_from_node_credential_name`, an exact parse to one plain segment.
    """
    # Interleaves DB work with blocking I/O (opening the sealed key may call KMS): a worker thread on its own
    # session (`on_thread`).
    _require_derived_node(caller)
    # The strict parser on the caller's raw slug: a composer that stripped whitespace first would
    # give one account two spellings (see `slug_from_node_credential_name`).
    parsed = account_auth.slug_from_node_credential_name(account_auth.NODE_CREDENTIAL_PREFIX + slug)
    account = session.scalar(
        select(Account).where(
            Account.slug == parsed,
            Account.enabled.is_(True),
            Account.auth_kind == account_auth.AUTH_KIND_API_KEY,
            Account.credential_id.is_not(None),
            # The fifth conjunct binds the caller to the account; the daemon's per-pane lease check is the
            # first wall. It is NODE-granular: a stolen node token reads every API-key account leased to a
            # task-held slot on this node or console-leased to it within 24 h (N accounts, not one), confined to one tenant by the read seam's org
            # bind (`tests/test_api_key_cross_org.py`, G3). It is not per-task isolation and it is not node-proof.
            # PER-TASK NARROWING WAS CONSIDERED AND REJECTED (D7): a daemon-stamped `X-Agent-Task-Id` plus
            # `Worktree.task_id == <declared>` buys NOTHING, because the declaration is the caller's own, so it
            # is a spelling of the same wall. The two checks are layered; they are simply not both node-proof.
            # `state = 'task'` excludes stale `free` slots;
            # `leased_account_id` (CP-written at lease, `slot_state.bind_account`) not the sensed `account_id`,
            # empty at `launch.sh` time.
            or_(
                Account.id.in_(
                    select(Worktree.leased_account_id).where(
                        Worktree.node_id == caller.node_id,
                        Worktree.state == slot_state.TASK,
                    )
                ),
                # The second arm: an unexpired console key lease this node holds (`app/console_key_leases.py`),
                # written only when a console create picked this API-key account for this node.
                Account.id.in_(console_key_leases.live_account_ids(caller.node_id)),
            ),
        )
    ) if parsed else None
    if account is None:
        # The log carries the validated `parsed` slug (bounded, unlike the raw path parameter); the response
        # carries neither slug nor reason.
        log.warning("account api-key: refused slug=%r to node=%s", parsed or "<unparseable>",
                    caller.node_id)
        raise HTTPException(status_code=404, detail="credential not available")
    credential = session.get(Credential, account.credential_id)
    # The key is a sealed field, opened here (`credential_crypto.open_secret`, the one private operation
    # on this path) because this route is where it is consumed.
    api_key = ""
    if credential is not None and has_sealed_secret(credential.config or {}, account_auth.API_KEY_CONFIG_KEY):
        try:
            api_key = open_secret(credential, account_auth.API_KEY_CONFIG_KEY)
        except ValueError:
            # A moved, re-pointed or tampered envelope: the same undisclosing 404 below, with the cause
            # (never the value) in the log.
            log.warning("account api-key: account %s holds a key that cannot be opened — rotate it", parsed)
    if not api_key:
        # The same undisclosing 404: a distinct status would confirm the slug is a real, enabled account this
        # node holds. The full reason goes to the log below.
        log.warning(
            "account api-key: account %s is an API-key account but its credential holds no key — "
            "refused as unavailable; rotate the key on the Runners tab to restore it", parsed)
        raise HTTPException(status_code=404, detail="credential not available")
    # Account and node, never the key. `api_key` must never be interpolated into any log line here.
    log.info("account api-key: served account=%s node=%s", parsed, caller.node_id)
    # `credential` is not None here (a key was opened from it); the workspace is tier-1 plaintext in its config,
    # served only when well-formed: it becomes a request header on the node.
    workspace_id = (credential.config or {}).get(account_auth.WORKSPACE_ID_CONFIG_KEY)
    if not isinstance(workspace_id, str) or not account_auth.is_valid_workspace_id(workspace_id):
        workspace_id = ""
    out = AccountApiKeyOut(api_key=api_key, workspace_id=workspace_id)
    credential_health.record_use(session, [account.credential_id])
    return out


@router.get("/bedrock-credentials")
@infra(Tenancy.ORG_SCOPED)
@on_thread
def get_bedrock_credentials(
    session: Session,
    door: AgentDoor,
    name: str = "",
) -> JSONResponse:
    """K3: 15-minute, invoke-only Bedrock credentials for one open Jaah session routed to THIS node
    (`app/jaah_routing/credentials.py` decides). 200 `{"Credentials": {...}}` — Claude Code's
    `awsCredentialExport` shape; 403 `{"reason": ...}`. `_require_derived_node`: a shared token names no node,
    and the node IS the binding. The answer is never logged and never cached."""
    # Interleaves DB work with blocking I/O (the STS mint on every answer): a worker thread on its own session (`app/db_async.py::run_blocking_in_session`).
    if not edition_features.enabled("jaah_ai_credit"):
        raise HTTPException(status_code=404, detail=_NO_JAAH_ACCOUNT)
    from app import bedrock
    from app.jaah_routing import credentials as jaah_credentials

    caller, x_agent_task_id = door
    _require_derived_node(caller)
    answer = jaah_credentials.issue(
        session, name=name, node_id=caller.node_id, node_org_id=caller.org_id, task_header=x_agent_task_id,
    )
    headers = {"Cache-Control": "no-store"}
    if isinstance(answer, jaah_credentials.Refusal):
        log.info("bedrock credentials: refused %s to node=%s", answer.reason, caller.node_id)
        return JSONResponse(status_code=403, content={"reason": answer.reason}, headers=headers)
    out = JSONResponse(status_code=200, content=answer, headers=headers)
    # The platform credential the session was minted from; none under `task_role` (`bedrock.Connection`).
    # Best-effort like the stamp itself: the credentials are minted, so nothing here may turn them into a 500.
    try:
        conn = bedrock.connection(session)
    except Exception:  # noqa: BLE001 - see above
        log.warning("bedrock credentials: platform credential not stamped (settings unreadable)")
        conn = None
    if conn is not None and conn.credential_id is not None:
        credential_health.record_use(session, [conn.credential_id])
    return out


class JaahUsageTokensIn(StrictModel):
    """One call's tokens in the five billing classes (`app/model_prices.py::TOKEN_CLASSES`); an absent class is 0."""

    # Bounded far above any one call (a 1M-token context): a count past it is a fault, refused before pricing.
    input: int = Field(default=0, ge=0, le=100_000_000)
    cache_write_5m: int = Field(default=0, ge=0, le=100_000_000)
    cache_write_1h: int = Field(default=0, ge=0, le=100_000_000)
    cache_read: int = Field(default=0, ge=0, le=100_000_000)
    output: int = Field(default=0, ge=0, le=100_000_000)


class JaahUsageCallIn(BaseModel):
    call_key: str = Field(min_length=3, max_length=200)
    request_id: Optional[str] = Field(default=None, max_length=200)
    message_id: Optional[str] = Field(default=None, max_length=200)
    model: str = Field(min_length=1, max_length=512)
    occurred_at: datetime
    tokens: JaahUsageTokensIn

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must carry a timezone")
        return value


#: K4's per-post ceiling (the node's `bedrock_usage_tail.MAX_BATCH`).
MAX_USAGE_CALLS = 200
#: K3 and K4 in an edition without the Jaah account: no Jaah session exists to serve.
_NO_JAAH_ACCOUNT = "unknown session"


class JaahUsageIn(BaseModel):
    calls: list[JaahUsageCallIn] = Field(max_length=MAX_USAGE_CALLS)


class JaahUsageOut(BaseModel):
    accepted: int
    duplicates: int
    #: Calls on a model with no Bedrock rate: not charged, and the operator is alerted (AS9).
    unpriced: int = 0


@router.post("/jaah-sessions/{jaah_session_id}/usage", response_model=JaahUsageOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def post_jaah_session_usage(
    session: Session,
    jaah_session_id: UUID,
    body: JaahUsageIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> JaahUsageOut:
    """K4: the node's live report of one Jaah session's model calls, each metered and debited at once
    (`app/token_billing/metering.py::ingest` decides). 404 for a session that is not this node's, 410 for one
    ended over an hour ago — the AWS correction bills what this refuses. `_require_derived_node`: the node is
    the binding."""
    if not edition_features.enabled("jaah_ai_credit"):
        raise HTTPException(status_code=404, detail=_NO_JAAH_ACCOUNT)
    from app.token_billing import metering

    _require_derived_node(caller)
    calls = [
        metering.NodeCall(call_key=call.call_key, model=call.model, occurred_at=call.occurred_at,
                          tokens=call.tokens.model_dump())
        for call in body.calls
    ]
    try:
        result = metering.ingest(
            session, jaah_session_id=jaah_session_id, node_id=caller.node_id, node_org_id=caller.org_id, calls=calls,
        )
    except metering.NotTaken as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    return from_row(JaahUsageOut, result)


# ── Task-scoped credential access: the console's OWN credential rows ──────────
# Serves the operator-managed `credentials` table; no code path is shared with the routes above. The
# reach is the task launcher's (`_task_grant_principal`) on the TASK'S project, per
# `authz.task_roster_select`: its own credentials, the project's attached pool under `credentials:view`,
# and org credentials under an org role (E1, AC-3.2).
class TaskCredentialSummary(BaseModel):
    """One credential a task may reach, with no secret in it. `config` is absent, not stripped, to avoid a
    second serializer beside `secret_keys()`.

    `fields` is the type's declared field keys (`credentials/registry.py::field_keys`), schema only, so
    runners can name a field (`routes/agent_project_settings.py::AgentEnvVarWriteIn.credential_field`)
    without pulling secrets via `credential.get`.
    """

    # The scope-relative path (`runner_path`): `org/<name>`, `projects/<project>/<name>` or `me/<name>`.
    name: str
    type: str
    type_label: str = ""
    description: str = ""
    #: Declaration order. Empty for a type that left the registry (see `field_keys`).
    fields: list[str] = []


class TaskCredentialListOut(BaseModel):
    credentials: list[TaskCredentialSummary]


class TaskCredentialOut(BaseModel):
    """One credential's full configuration — SECRETS INCLUDED, which is the point of the route. Never log
    `config`, never put it in an event payload (`app/models/credentials.py::Credential`)."""

    # The scope-relative path it was fetched by (`runner_path`).
    name: str
    type: str
    config: dict[str, Any]
    # A live access token for an OAuth2 credential, so consumers need not implement RFC 6749 §6 refresh
    # themselves (`oauth2_tokens.py` owns the exchange, cache and row lock). Absent is normal: CLI-minted rows
    # have no client of ours, and revoked grants answer None. `config` is served unchanged either way.
    access_token: Optional[str] = None
    # ISO-8601 expiry. None means no `expires_in` was sent: a non-expiring token.
    access_token_expires_at: Optional[str] = None
    # A NodeVault credential's secret fields, STILL SEALED: `{field: {envelope: {v,k,alg,ek,n,ct}, aad:
    # <base64>}}`, and `config` omits them. The node's broker opens each with the key its `k` names and
    # merges the plaintext into `config` before the runner sees it (`cred_broker._open_nodevault`). Empty
    # for every other credential.
    nodevault: dict[str, dict[str, Any]] = {}


def _task_grant_principal(session: Session, task: Task) -> Principal | None:
    """The launcher principal whose bindings decide what `task`'s runner may reach, or None when the chain
    names nobody.

    The ancestry walk (`app/task_ancestry.py`), not `task.created_by_id`: pipeline-filed children
    may lack a creator, and the walk costs one hop when the column is set.

    None means deny here, the opposite of placement (`placement_grants.allowed_accounts`, where it means
    the org floor). Callers must branch on None and never pass it on: `authz/` reads a None principal as
    allow-all.
    """
    launcher_id = launcher_user_id(session, task.id)
    if launcher_id is None:
        return None
    user = session.get(AdminUser, launcher_id)
    if user is None:
        return None
    return principal_for_user(session, user)


def runner_path(row: Any, principal_user_id: UUID, project_names: Mapping[int, str]) -> str:
    """The scope-relative path a runner addresses a granted credential by (`Credential`'s NAMING) — the ONE
    place it is computed: `me/<name>` for the launcher's own row, `projects/<lowercased project name>/<name>`
    for a row homed in a project, else `org/<name>`. `row` is a `Credential` or a row of its `name`,
    `owner_id` and `home_project_id` columns; `project_names` maps home project ids to names (`entity_shape.names_of`).
    A personal row on a task's roster is always the launcher's (`usable_credential_clause`). The project
    segment is percent-escaped (`%` as `%25`, then `/` as `%2f`), so it stays ONE segment and project `a/b`'s
    `x` can never share a path with project `a`'s `b/x`."""
    if row.owner_id is not None and row.owner_id == principal_user_id:
        return f"me/{row.name}"
    if row.home_project_id is not None:
        project = project_names.get(row.home_project_id, str(row.home_project_id))
        segment = project.lower().replace("%", "%25").replace("/", "%2f")
        return f"projects/{segment}/{row.name}"
    return f"org/{row.name}"


def _granted_credentials(
    session: Session, task: Task, path: str | None = None
) -> list[tuple[str, Credential]]:
    """Every credential `task`'s launcher may use on the task's project with its `runner_path`, newest
    first, narrowed to the rows whose path is `path` (case-insensitively) when given. Empty when no launcher
    principal resolves.

    A by-path fetch matches over the roster's name and home columns alone and then loads only the matches,
    so it never loads or decrypts other rows; two usable rows sharing a path both arrive for the caller to
    refuse.
    """
    principal = _task_grant_principal(session, task)
    if principal is None:
        return []
    return granted_to(session, principal, task.project_id, path)


def granted_to(
    session: Session, principal: Principal, project_id: Optional[int], path: str | None = None
) -> list[tuple[str, Credential]]:
    """`_granted_credentials` for a task on `project_id` that `principal` would launch: the broker's one rule,
    also what a pipeline save checks a gate's or target's credential against (`app/releases/pipeline.py`)."""
    if principal is None:  # never `authz`'s allow-all reading of None
        return []
    # The rule lives in `authz.task_roster_select` (also used by project-env resolution), with both
    # subtractions (no superadmin bypass; no account API key, by reference). Never copy it.
    stmt = task_roster_select(principal, project_id).order_by(
        Credential.created_at.desc(), Credential.id.desc()
    )
    if path is not None:
        heads = session.execute(
            stmt.with_only_columns(
                Credential.id, Credential.name, Credential.owner_id, Credential.home_project_id
            )
        ).all()
        names = names_of(session, Project, (h.home_project_id for h in heads))
        wanted = path.lower()
        ids = [h.id for h in heads if runner_path(h, principal.user_id, names).lower() == wanted]
        if not ids:
            return []
        stmt = stmt.where(Credential.id.in_(ids))
    rows = list(session.scalars(stmt))
    names = names_of(session, Project, (r.home_project_id for r in rows))
    return [(runner_path(row, principal.user_id, names), row) for row in rows]


@router.get("/tasks/{task_id}/credentials", response_model=TaskCredentialListOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_task_credentials(
    session: Session,
    task_id: int,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> TaskCredentialListOut:
    """The credentials this task may reach — path (as `name`, `runner_path`), type and description only,
    never a secret.

    Lets a runner discover what it holds (see `portfolio/fleet/runner-charter.md` §Credentials).
    A carried-over runner is refused: the new org's credentials are not its to list.
    """
    _refuse_a_carried_over_caller(caller)
    task = auth._task_or_404_for_node(session, caller, task_id)
    return TaskCredentialListOut(
        credentials=[
            TaskCredentialSummary(
                name=path,
                type=row.type,
                type_label=type_label(row.type),
                description=row.description or "",
                fields=list(field_keys(row.type)),
            )
            for path, row in _granted_credentials(session, task)
        ]
    )


def _enrol_on_nodevault_miss(
    session: Session, node_id: UUID, nodevault: dict[str, dict[str, Any]]
) -> None:
    """After a NodeVault fetch is recorded: when `node_id` holds no `delivered` grant for a key the served
    envelopes name, `ensure_grants` (re)starts its delivery, so the broker's `nodevault_key_missing` heals
    itself (AC-5.7). Best-effort — the fetch is already committed and the node's `hello` re-runs enrolment."""
    hashes = {
        credential_keys.nodevault_key_hash_of(str(entry["envelope"].get("k", "")))
        for entry in nodevault.values()
    } - {None}
    try:
        delivered = set(session.scalars(
            select(NodeVaultGrant.key_hash).where(
                NodeVaultGrant.node_id == node_id,
                NodeVaultGrant.key_hash.in_(hashes),
                NodeVaultGrant.state == NV_GRANT_DELIVERED,
            )
        ))
        if hashes <= delivered:
            return
        nodevault_grants.ensure_grants(session, node_id)
        session.commit()
    except Exception:  # noqa: BLE001 — the fetch stands; `hello` re-runs enrolment
        session.rollback()
        log.warning("NodeVault enrolment of node %s after a fetch miss failed", node_id, exc_info=True)


@router.get("/tasks/{task_id}/credential", response_model=TaskCredentialOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def get_task_credential(
    session: Session,
    task_id: int,
    name: str = Query(
        ..., min_length=1, description="the credential's scope-relative path, as the roster reports it"
    ),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> TaskCredentialOut:
    """Hand back one granted credential's configuration, secrets included.

    `name` is the scope-relative path (`runner_path`). 404, never 403, for both a nonexistent and an
    ungranted path: the per-task roster must not be enumerable. An ambiguous path (a duplicate that predates
    the per-home rule, `app/models/credentials.py::Credential`) resolves to nothing rather than serving an
    arbitrary row.
    """
    # Interleaves DB work with blocking I/O (an OAuth2 refresh, a KMS unwrap, a NodeVault grant workflow): a
    # worker thread on its own session (`on_thread`).
    _refuse_a_carried_over_caller(caller)
    task = auth._task_or_404_for_node(session, caller, task_id)

    matches = _granted_credentials(session, task, path=name)
    if not matches:
        log.warning(
            "task credential: refused path=%r for task=%s (no such credential, or no grant)",
            name, task_id,
        )
        raise HTTPException(status_code=404, detail=f"no credential at path {name!r} available to this task")
    if len(matches) > 1:
        log.warning(
            "task credential: refused path=%r for task=%s (%d granted rows share it)",
            name, task_id, len(matches),
        )
        raise HTTPException(
            status_code=409,
            detail=f"{len(matches)} credentials share the path {name!r} here — rename them to disambiguate",
        )

    path, row = matches[0]
    if row.needs_reconnect_at is not None:
        # 410, not a stale token: the grant is dead (a refused refresh or a revocation the vendor reported) and only
        # a person pressing Connect revives it; 409 already means an ambiguous path. AC1.4: never a silent failure.
        log.warning("task credential: refused path=%s for task=%s (needs reconnect)", path, task_id)
        raise HTTPException(
            status_code=410,
            detail=f"credential {path!r} needs reconnect - {row.needs_reconnect_reason or 'its grant was revoked.'}"
            " Ask its owner to press Connect on it.",
        )
    # A Connect-minted forge grant (GitHub App user token, GitLab, Bitbucket) carries `token`/`token_expires_at`,
    # which `oauth2_tokens` does not read: renew it the way a git use would, before the config is opened, so the
    # runner never gets a token the control plane would have renewed.
    try:
        git_access.renew_if_expiring(session, row)
    except git_access.GitAccessNeedsReconnect as exc:
        log.warning("task credential: refused path=%s for task=%s (forge grant dead)", path, task_id)
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except git_access.GitAccessError as exc:
        # Retryable (`token_for`): a 503, never the expired token it could not renew. The message names the credential
        # and the forge's bounded text, never a token.
        log.warning("task credential: renewal failed path=%s task=%s reason=%s", path, task_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — an unreachable durable engine is retryable too, never a 500
        log.warning("task credential: renewal failed path=%s task=%s", path, task_id, exc_info=True)
        raise HTTPException(
            status_code=503, detail=f"could not renew credential {path!r} right now; try again shortly"
        ) from exc
    # Minted before the config is opened: a refresh commits the new token on its own session and expires
    # `config` here, so opening first would serve a stale `access_token` beside the fresh one. None for
    # non-OAuth2 and unmintable rows (`TaskCredentialOut.access_token`).
    access = oauth2_tokens.access_token_for(session, row)
    # Name and type only, never values or config keys.
    log.info("task credential: served path=%s type=%s task=%s", path, row.type, task_id)
    # The runner hand-off opens the whole row: every sealed secret, one private operation per secret field
    # (`credential_crypto.opened_config`), never off the ORM. The `credential_read` row is COMMITTED before
    # the config is returned, so a failed record is a 500 rather than an unrecorded read.
    # A NodeVault field is never opened here (the CP cannot): it leaves sealed, with its AAD, for the
    # node's broker to open (AC-5.6). Never plan-gated (D11): a frozen org's credentials still fetch.
    config = data_access_events.opened_config_for(
        session, row, consumer="task", task_id=task_id, node_id=caller.node_id, keep_nodevault=True
    )
    config, nodevault = credential_crypto.nodevault_wire(row, config)
    provider = connect_for_type(row.type)
    if provider is not None and (oauth_providers.uses_platform_client(provider, config) or provider.discovery):
        # The deployment's app renews this grant, and a runner needs only the access token beside it: a refresh
        # token leaked from a runner would otherwise stay renewable by anyone holding the app's client. A
        # discovery provider's (an MCP server's) renews on the control plane too; its bridge reads the access token.
        config.pop("refresh_token", None)
        if provider.discovery:
            config.pop("client_secret", None)
    if git_access.is_connect_grant(row):
        # Renewed on the control plane above; a forge's refresh token is single-use, so one a runner spent would
        # kill the grant for everyone.
        config.pop("refresh_token", None)
    # Read before the commit: a failed enrolment's rollback expires `row`, and the answer must not need it.
    row_type, row_id = row.type, row.id
    session.commit()
    if nodevault and caller.node_id is not None:
        _enrol_on_nodevault_miss(session, caller.node_id, nodevault)
    out = TaskCredentialOut(
        name=path,
        type=row_type,
        config=config,
        nodevault=nodevault,
        access_token=access.token if access is not None else None,
        access_token_expires_at=(
            access.expires_at.isoformat()
            if access is not None and access.expires_at is not None
            else None
        ),
    )
    credential_health.record_use(session, [row_id])
    return out
