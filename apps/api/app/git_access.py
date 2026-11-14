"""Whose git credential a piece of work uses on its repo's forge, and the login to send.

The single home for that answer: the node's git askpass, the pool clone and the docs reader all resolve
here. There is no deployment-wide git credential; do not reintroduce one under any name. The PERSON is
chosen by this rule, in order, and the credential is that person's connection for the repo's forge root
(`forges.RepoRef.root`; `models.UserGitConnection` — of several, the one whose account reaches the repo's
owner, else the root's default):
1. a human started it (`task_ancestry.starter_user_id`) -> that person's connection, else refuse;
2. no starter and a schedule, trigger or automation filed it -> the project's "Run jobs as" person
   (`project_forge_person`), never the row's creator: nobody's `created_by_id` decides whose login automated work
   spends;
3. anything else (system alarm, system-filed fan-out) -> refuse.
Branch 2 is gated on the machine marker, never on "a creator exists". A runner-filed task takes branch 1: a
top-level filing is stamped `started_by_id` = its chain's executor (`task_ancestry.executor_user_id`), and a
pipeline child walks to its epic's starter. The GitHub App and the GitHub OAuth App are one operator-plane row
each with no org; `git_connect_app` picks which one new grants come from (`app_config`), and a stored
token of either kind keeps working.
Selections are re-authorized on every read; refresh is locked and durable (`app/durable/git_token.py`).
Nothing here logs a token.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from functools import partial
from typing import Literal, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forge_oauth, forges, git_probe, github_oauth
from app.clock import as_utc, utcnow
from app.operator_settings import reads as settings_reads
from app.authz.principal import principal_for_user
from app.authz.visibility import git_identity_select
from app.credential_crypto import has_sealed_secret, open_secret
from app.credentials.registry import type_label
from app.credentials.testers import AZURE_DEVOPS_KINDS, BITBUCKET_KINDS
from app.forges import FORGE_LABELS, RepoRef
from app.models.credentials import Credential, UserGitConnection
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.task_ancestry import starter_user_id
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

log = logging.getLogger("app.git_access")

# Types that can authenticate a git remote, one per forge (`forges.FORGES`); resolution refuses anything
# else. `github_app` and `github_install_app` are absent: they authenticate no remote and are reachable only
# through their operator knobs (`APP_KNOBS`).
GIT_CREDENTIAL_TYPES: frozenset[str] = frozenset({"github", "gitlab", "bitbucket", "azure_devops"})

#: The credential types whose OAuth grant this release renews — one per `forge_oauth.FORGE_OAUTH` entry —
#: each through the rotation `app/durable/git_token.py::ROTATIONS` registers for it (held equal by a
#: test). Renewed only on the spec's own root (`_needs_refresh`): self-managed GitLab has no Connect (D4).
REFRESHABLE_TYPES: frozenset[str] = frozenset(forge_oauth.FORGE_OAUTH)

# Refresh this long before expiry so a token cannot expire mid-clone.
_REFRESH_MARGIN = timedelta(minutes=10)


class GitAccessError(RuntimeError):
    """No usable git credential could be resolved, carrying an operator-facing reason.

    Every message names what to change (a person, a setting, a credential); the readers have nowhere
    else to look.
    """


class GitAccessNeedsReconnect(GitAccessError):
    """The credential's grant is dead (its refresh token was refused, or its forge reported the token
    revoked); only the user can fix it by reconnecting."""


class GitLogin(NamedTuple):
    """How a git client authenticates to a forge: HTTP `scheme`, and for `basic` the username sent with
    `token` as the password. `api_username` is the Basic username the forge's REST API takes with the same
    token where that differs from git's (`_REST_USERNAME_FIELDS`), else `""`. Never log `token`."""

    scheme: Literal["basic", "bearer"]
    username: str
    token: str
    api_username: str = ""


# THE USERNAME TABLE, single-homed: `(credential type, kind) -> (scheme, username)`. A forge ignores the
# username only where it says so; Bitbucket and Azure DevOps key on it. `kind` is the credential's own
# field on Bitbucket and Azure DevOps (`credentials/testers.py::BITBUCKET_KINDS`/`AZURE_DEVOPS_KINDS`,
# first = default); GitHub and GitLab send one username for PATs and OAuth tokens alike.
_GIT_LOGINS: dict[tuple[str, str], tuple[Literal["basic", "bearer"], str]] = {
    ("github", ""): ("basic", "x-access-token"),
    ("gitlab", ""): ("basic", "oauth2"),
    ("bitbucket", "oauth"): ("basic", "x-token-auth"),
    ("bitbucket", "api_token"): ("basic", "x-bitbucket-api-token-auth"),
    ("azure_devops", "pat"): ("basic", "jaah"),
    # A Microsoft Entra token is not accepted as a Basic password: git sends it as a bearer header.
    ("azure_devops", "oauth"): ("bearer", ""),
}
# THE REST USERNAME RULE, single-homed beside the table above: `(credential type, kind) -> config key`
# whose value is the Basic username the forge's REST API takes with the token, where it is not git's.
# Bitbucket accepts an API token over REST only as `<Atlassian account email>:<token>`
# (`credentials/testers.py::test_bitbucket` authenticates the same way); git keeps its fixed username.
# A row missing that value is refused, never served with a placeholder (`rest_username_problem`).
_REST_USERNAME_FIELDS: dict[tuple[str, str], str] = {
    ("bitbucket", "api_token"): "username",
}
_KINDS: dict[str, tuple[str, ...]] = {"bitbucket": BITBUCKET_KINDS, "azure_devops": AZURE_DEVOPS_KINDS}


def _kind_of(credential_type: str, config: dict) -> str:
    kinds = _KINDS.get(credential_type)
    if kinds is None:
        return ""
    raw = config.get("kind")
    return raw if isinstance(raw, str) and raw in kinds else kinds[0]


def credential_kind(credential: Credential) -> str:
    """The row's token kind for the username table: `""` for a forge with one, else its `kind` field,
    defaulting to the pasted-token kind."""
    return _kind_of(credential.type, credential.config or {})


def rest_username_problem(credential_type: str, config: dict) -> Optional[str]:
    """The config key a credential of this type and kind must carry for its REST username, when it is
    blank; else None. Read at save (`routes/credentials/`) and at the broker (`git_login_for`)."""
    key = _REST_USERNAME_FIELDS.get((credential_type, _kind_of(credential_type, config)))
    if key is None:
        return None
    value = config.get(key)
    return key if not isinstance(value, str) or not value.strip() else None


def credential_root(credential: Credential) -> Optional[str]:
    """The forge root `credential` authenticates (`forges.credential_root`), or None for a non-git type."""
    base_url = (credential.config or {}).get("base_url")
    return forges.credential_root(credential.type, base_url if isinstance(base_url, str) else None)


def profile_path(forge: str) -> str:
    return f"Profile > Git > {FORGE_LABELS[forge]}"


#: The provenance columns marking a row a schedule, trigger or automation filed (`git_person_for_task` branch 2).
_AUTOMATED_ORIGIN_COLUMNS = ("created_by_cron_id", "created_by_trigger_id", "created_by_automation_id")


def automated_run_as(session: Session, task: Task) -> Optional[UUID]:
    """The "Run jobs as" person of an automation-filed `task`'s project (`project_forge_person`), or None when a
    person filed it or its project is gone. Reads this one row's marker, not the chain: a marker is a fact about
    one row."""
    if not any(getattr(task, column) is not None for column in _AUTOMATED_ORIGIN_COLUMNS):
        return None
    project = session.scalars(select(Project).where(Project.id == task.project_id)).first()
    return project_forge_person(session, project) if project is not None else None


def git_person_for_task(session: Session, task: Task) -> Optional[UUID]:
    """Who does `task`'s git work, or None (branches 1-2 of the module rule).

    Starter first and absolutely. Both `resolve_for_task` and `commit_identity_for_task` read this, so
    a commit can never name a different person from the push that carries it.
    """
    starter_id = starter_user_id(session, task.id)
    if starter_id is not None:
        return starter_id
    run_as_id = automated_run_as(session, task)
    if run_as_id is not None:
        log.debug("git-access: task=%s is automation-filed, using its project's Run jobs as %s", task.id, run_as_id)
    return run_as_id


#: The live tasks `live_runner_person` looks through, newest first.
_LIVE_RUNNER_TASKS = 20


def live_runner_person(session: Session, project_id: int, *, exclude_persona: str) -> Optional[UUID]:
    """The git person of the newest live (`active`/`awaiting_response`) task of the project whose persona is not
    `exclude_persona` (the caller's own system persona), or None when no live task names one."""
    tasks = session.scalars(
        select(Task).where(
            Task.project_id == project_id,
            Task.persona.is_distinct_from(exclude_persona),
            Task.state.in_(("active", "awaiting_response")),
        ).order_by(Task.id.desc()).limit(_LIVE_RUNNER_TASKS)
    ).all()
    for task in tasks:
        person = git_person_for_task(session, task)
        if person is not None:
            return person
    return None


def usable_job_person(session: Session, user_id: Optional[UUID], org_id: UUID) -> bool:
    """Whether `user_id` can be a "Run jobs as" person of org `org_id`: a real, enabled member of that org."""
    return user_id is not None and session.scalar(select(AdminUser.id).where(
        AdminUser.id == user_id, AdminUser.is_system.is_(False), AdminUser.org_id == org_id,
        AdminUser.disabled_at.is_(None),
    )) is not None


def explicit_job_people(session: Session, project: Project, *, run_as_id: Optional[UUID] = None) -> list[UUID]:
    """The usable people named for `project`'s background jobs, in order: `run_as_id` (an automation's own "Run
    as"), the project's "Run jobs as" (`projects.run_jobs_as_id`), the org's (`orgs.run_jobs_as_id`)."""
    picks = (run_as_id, project.run_jobs_as_id, session.scalar(select(Org.run_jobs_as_id).where(Org.id == project.org_id)))
    return list(dict.fromkeys(p for p in picks if p is not None and usable_job_person(session, p, project.org_id)))


def has_forge_login(session: Session, user_id: UUID, project: Project) -> bool:
    """Whether `user_id` holds a live git connection (its credential not marked for a reconnect) on `project`'s forge
    root (`forges.RepoRef.root`); any live git connection when the project names no usable repository URL, since
    there is no root to match."""
    query = (
        select(UserGitConnection.user_id)
        .join(Credential, Credential.id == UserGitConnection.credential_id)
        .where(UserGitConnection.user_id == user_id, Credential.needs_reconnect_at.is_(None))
    )
    try:
        query = query.where(UserGitConnection.root == forges.repo_ref(project).root)
    except forges.ForgeUrlError:
        pass
    return session.scalar(query.limit(1)) is not None


def project_forge_person(session: Session, project: Project, *, run_as_id: Optional[UUID] = None) -> Optional[UUID]:
    """THE "Run as" rule: whose forge login and coding-agent account a project's background work uses — the merge
    lane's PRs no runner reports, the outcome scan's reads, benchmark reviews, memory graduations and every
    automation's tasks. Candidates in order: `explicit_job_people`, then the org's live Owners/Admins by email. The
    first holding a git login on the project's forge root (`has_forge_login`) wins: a pick without one is skipped,
    since a task started as them cannot push. Nobody holds one: the first candidate anyway, so the work still starts
    and its refusal names who must connect. A departed or disabled pick is never a candidate. None only when nothing
    is picked and the org has no live Owner/Admin."""
    from app.access import owners_and_admins

    explicit = explicit_job_people(session, project, run_as_id=run_as_id)
    for person_id in explicit:
        if has_forge_login(session, person_id, project):
            return person_id
    admins = [p.id for p in owners_and_admins(session, project.org_id)] if project.org_id is not None else []
    for person_id in admins:
        if person_id not in explicit and has_forge_login(session, person_id, project):
            return person_id
    candidates = explicit + admins
    return candidates[0] if candidates else None


def requested_job_person(session: Session, project: Project, requester_id: Optional[UUID]) -> Optional[UUID]:
    """Whom a control-plane run of `project` that `requester_id` asked for (None: the system) starts as: the requester
    when they are a usable job person holding a git login on the project's forge (`has_forge_login`), else the
    project's "Run as" person (`project_forge_person`). A hidden run filed with no starter clones as nobody (branch 3
    of the module rule), so every such run is stamped with this person."""
    if (
        requester_id is not None
        and usable_job_person(session, requester_id, project.org_id)
        and has_forge_login(session, requester_id, project)
    ):
        return requester_id
    return project_forge_person(session, project)


def project_repo(project: Optional[Project], *, retry: str) -> RepoRef:
    """`project`'s repository (`forges.repo_ref`), or a `GitAccessError` saying the URL must be fixed."""
    if project is None:
        raise GitAccessError(f"The project this work belongs to no longer exists, {retry} on a current project.")
    try:
        return forges.repo_ref(project)
    except forges.ForgeUrlError as exc:
        raise GitAccessError(
            f"Project '{project.name}' has no usable repository URL ({exc.problem}), so there is no "
            f"forge to authenticate to. Fix its Repo URL on the project's page, {retry}."
        ) from None


def task_repo(session: Session, task: Task) -> RepoRef:
    """The repository `task`'s git work runs against: its project's (`project_repo`)."""
    project = session.scalars(select(Project).where(Project.id == task.project_id)).first()
    return project_repo(project, retry=_RETRY_TASK)


def resolve_for_task(session: Session, task: Task, repo: Optional[RepoRef] = None) -> Credential:
    """The credential `task` should use for its project's forge root, or `GitAccessError` naming what to
    fix (`git_person_for_task`). `repo` is `task_repo(session, task)` unless the caller already has it."""
    repo = repo if repo is not None else task_repo(session, task)
    person_id = git_person_for_task(session, task)
    if person_id is not None:
        return resolve_for_user(session, person_id, repo)
    label = FORGE_LABELS[repo.forge]
    raise GitAccessError(
        f"Task {task.id} has no starter, so there is no {label} account to do its git work as. If a "
        "person filed it, start it from the console (Send to build, Start, or Retry) so the person "
        f"starting it is recorded and their connected {label} account for {repo.root} is used. If a "
        "machine filed it, only a scheduled activity carries an owner this can fall back to."
    )


def commit_identity_for_task(session: Session, task: Task) -> Optional[dict[str, str]]:
    """`{"name", "email"}` for the person `git_person_for_task` names, or None when it names nobody.

    The console email is what a forge links a commit to an account by, on every forge; the name falls
    back to the email's local part. Never raises; None leaves git config to decide."""
    person_id = git_person_for_task(session, task)
    user = session.get(AdminUser, person_id) if person_id is not None else None
    if user is None or not user.email:
        return None
    name = " ".join(part for part in (user.first_name, user.last_name) if part and part.strip())
    # Sanitised here: the node drops an ident carrying a control char or `<>` whole (a newline would
    # forge a second ident line), which would fall back to the shared clone's `user.*`.
    name = " ".join(re.sub(r"[\x00-\x1f\x7f<>]", " ", name).split())
    return {"name": name or user.email.split("@", 1)[0], "email": user.email}


# How a refusal tells the reader to retry. A parameter because pool clones and the docs browser have
# no task to restart, and `GitAccessError` messages must be actionable.
_RETRY_TASK = "then start the task again"
CLONE_RETRY = "then materialise the slot again (there is no task here to restart)"


def project_repo_ref(row: ProjectRepo, *, retry: str = _RETRY_TASK) -> RepoRef:
    """One connected repository's `RepoRef` — a `project_repos` row parses exactly as a project's
    primary does (`forges.repo_ref`) — or a `GitAccessError` saying its URL must be fixed."""
    try:
        return forges.repo_ref(row)
    except forges.ForgeUrlError as exc:
        raise GitAccessError(
            f"Repository '{row.name}' of this project has no usable URL ({exc.problem}), so there is no forge "
            f"to authenticate to. Fix it on the project's Repository line, {retry}."
        ) from None


def root_connections(session: Session, user: AdminUser, root: str) -> list[UserGitConnection]:
    """`user`'s connections on `root`, the default first, then oldest first."""
    return list(session.scalars(
        select(UserGitConnection)
        .where(UserGitConnection.user_id == user.id, UserGitConnection.root == root)
        .order_by(UserGitConnection.is_default.desc(), UserGitConnection.created_at, UserGitConnection.id)
    ).all())


def owner_match(path: str, owners: list[str]) -> int:
    """How long the longest of `owners` that owns `path` (`o/r`, `g/sub/r`, `org/project/r`) is; 0 for none."""
    lowered = path.lower()
    return max((len(o) for o in owners if o and lowered.startswith(o.lower() + "/")), default=0)


def _reach(session: Session, connections: list[UserGitConnection], path: str) -> list[int]:
    """Per connection, how specifically it reaches `path`'s owner (`owner_match`) through its recorded owners
    or its own forge account."""
    accounts = {
        row.id: (row.config or {}).get("account")
        for row in session.scalars(
            select(Credential).where(Credential.id.in_([c.credential_id for c in connections]))
        ).all()
    }
    reach = []
    for connection in connections:
        account = accounts.get(connection.credential_id)
        reach.append(owner_match(path, [*(connection.owners or []), *([account] if isinstance(account, str) else [])]))
    return reach


def pick_connection(
    session: Session, connections: list[UserGitConnection], path: str
) -> UserGitConnection:
    """Of one root's `connections` (default first, `root_connections`), the one for a repository at `path`:
    the account whose recorded owners or own forge account owns it — the most specific owner wins, a tie
    goes to the earlier row — else the default. A path-less ask (a whole root) is the default's."""
    if len(connections) == 1 or not path:
        return connections[0]
    reach = _reach(session, connections, path)
    return connections[reach.index(max(reach))]


def owner_known(session: Session, connections: list[UserGitConnection], path: str) -> bool:
    """Whether any of one root's `connections` is recorded as reaching `path`'s owner (`pick_connection` matched
    rather than fell back to the default)."""
    return max(_reach(session, connections, path), default=0) > 0


def selected_credential_id(session: Session, user: AdminUser, repo: RepoRef) -> Optional[UUID]:
    """Which credential `user` selected for `repo`: of their connections on its root, the one for its owner
    (`pick_connection`). None: nothing connected there."""
    connections = root_connections(session, user, repo.root)
    return pick_connection(session, connections, repo.path).credential_id if connections else None


def resolve_for_user(
    session: Session, user_id: UUID, repo: RepoRef, *, retry: str = _RETRY_TASK,
    credential_id: Optional[UUID] = None,
) -> Credential:
    """The credential `user_id` connected for `repo`'s forge root, re-checked against their current grants.

    The selection is read (`selected_credential_id`), then the row re-authorized, so a revoked or foreign
    row stops resolving while the selection still names it; a credential for another root is never
    returned, and a row marked as needing a reconnect (`grant_dead`) is `GitAccessNeedsReconnect`.
    Every refusal names the forge, the root, the person and where to fix it; `retry` closes each with an
    action this caller can take. `credential_id` names one of the person's connections on the root instead
    of selecting one (Profile > Git lists each account's own state).
    """
    label = FORGE_LABELS[repo.forge]
    where = profile_path(repo.forge)
    user = session.get(AdminUser, user_id)
    if user is None:
        raise GitAccessError(
            f"The user this work belongs to (id {user_id}) no longer exists, so their {label} "
            f"connection for {repo.root} cannot be used ({retry} as a current user)."
        )
    who = user.email or str(user.id)

    if credential_id is None:
        credential_id = selected_credential_id(session, user, repo)
    if credential_id is None:
        raise GitAccessError(
            f"{who} has not connected a {label} account for {repo.root}. Open {where} and connect one "
            f"or paste a token, {retry}."
        )

    credential = _readable_credential(session, user, credential_id)
    if credential is None:
        raise GitAccessError(
            f"{who}'s {label} connection for {repo.root} names a credential that is no longer available "
            f"to them — it was deleted, or it is no longer theirs. Open {where} and connect again, {retry}."
        )
    _require_git_type(credential, who=who, forge=repo.forge)
    if credential_root(credential) != repo.root:
        raise GitAccessError(
            f"{who}'s {label} connection for {repo.root} names credential '{credential.name}', which "
            f"authenticates {credential_root(credential)} instead. Open {where} and connect "
            f"{repo.root}, {retry}."
        )
    if grant_dead(credential):
        raise GitAccessNeedsReconnect(
            f"{who}'s {label} connection for {repo.root} (credential '{credential.name}') was revoked at "
            f"{label}, so it needs reconnecting. Open {where} and reconnect or paste a new token, {retry}."
        )
    return credential


def grant_dead(credential: Credential) -> bool:
    """Whether a git credential needs reconnecting: its `needs_reconnect_at` column, set when its forge reported
    the token revoked (`git_probe.mark_revoked`) and cleared by any newly entered token (`Credential.config`). The
    one reader of that fact for every forge."""
    return credential.needs_reconnect_at is not None


#: Where a person reconnects or adds their own git login: the console's Profile › Git.
PROFILE_GIT_URL = "/profile?tab=git-accounts"


def revoked_hold_fix_url(session: Session, task: Task, wait_reason: str, viewer_id: UUID) -> Optional[str]:
    """Profile › Git when `wait_reason` is `task`'s revoked-login hold (`revoked_holds`) and `viewer_id` holds that
    login (`git_person_for_task`), else None: the fix is that person's alone, so nobody else is sent to their own
    Profile, where nothing is revoked. One task's reads: the detail route only."""
    if revoked_holds(session, [task]).get(task.id) != wait_reason:
        return None
    return PROFILE_GIT_URL if git_person_for_task(session, task) == viewer_id else None


def revoked_holds(session: Session, tasks) -> dict[int, str]:
    """`{task id: wait reason}` for those of `tasks` whose git credential its forge reported revoked
    (`grant_dead`, `git_probe.mark_revoked`). Such a task is not dispatched — its runner would only be refused
    the token (410) and park on the question — so the project's work waits on one sentence instead; reconnecting
    clears the mark (`routes/credentials/writes.py`, `routes/git_connections.py`) and lifts the hold. Derived,
    never stored. The repo is read once per project and the credential once per person and root; any other
    resolution problem is not this hold's (the runner's own refusal names it). A merge-lane run is never held
    here, as the dispatcher never holds one (`routes/fleet.py::fleet_allocation`), nor a task on a Jaah-made
    repository, whose login is the console's GitHub App's, never a person's (`jaah_repos.runner_app_repo`)."""
    from app import jaah_repos
    from app.merge_lane_state import is_lane_persona

    tasks = [task for task in tasks if not is_lane_persona(task.persona)]
    if not tasks or not _any_grant_dead(session, {task.org_id for task in tasks}):
        return {}
    repos: dict[int, Optional[RepoRef]] = {}
    dead: dict[tuple[UUID, str], bool] = {}
    out: dict[int, str] = {}
    for task in tasks:
        # Fail open: this is a hint on the dispatch pass and the board, so one task's unexpected error skips
        # that task (its runner's own refusal names the problem) instead of breaking either.
        try:
            if task.project_id not in repos:
                project = session.get(Project, task.project_id)
                try:
                    found = project_repo(project, retry=_RETRY_TASK)
                except GitAccessError:
                    found = None
                if found is not None and jaah_repos.runner_app_repo(session, project, found) is not None:
                    found = None
                repos[task.project_id] = found
            repo = repos[task.project_id]
            person_id = git_person_for_task(session, task) if repo is not None else None
            if repo is None or person_id is None:
                continue
            key = (person_id, repo.root)
            if key not in dead:
                dead[key] = _grant_dead(session, person_id, repo)
            if dead[key]:
                out[task.id] = f"{FORGE_LABELS[repo.forge]} credential revoked — reconnect"
        except Exception:
            log.warning("revoked_holds: skipped task id=%s on an unexpected error", task.id, exc_info=True)
    return out


def _any_grant_dead(session: Session, org_ids: set) -> bool:
    """Whether any git credential in `org_ids` needs reconnecting (`grant_dead`) — `revoked_holds`' one-query
    short-circuit, so the usual pass (nothing revoked) costs this read alone. A column, so filtered in SQL and
    nothing sealed is opened."""
    return session.scalar(
        select(Credential.id).where(
            Credential.org_id.in_(org_ids), Credential.type.in_(GIT_CREDENTIAL_TYPES),
            Credential.needs_reconnect_at.is_not(None),
        ).limit(1)
    ) is not None


def _grant_dead(session: Session, user_id: UUID, repo: RepoRef) -> bool:
    """Whether the credential `user_id` selected for `repo.root` needs reconnecting (`grant_dead`) — the same row
    `resolve_for_user` would refuse with `GitAccessNeedsReconnect`."""
    user = session.get(AdminUser, user_id)
    credential_id = selected_credential_id(session, user, repo) if user is not None else None
    credential = session.get(Credential, credential_id) if credential_id is not None else None
    return bool(
        credential is not None and credential_root(credential) == repo.root
        and grant_dead(credential)
    )


def _readable_credential(
    session: Session, user: AdminUser, credential_id: UUID
) -> Optional[Credential]:
    """`credential_id`, but only if `user` may push as it right now (`authz.git_identity_select`, the same
    rule the Git Access dropdown offers); otherwise None. The retiring pointer is read only as that rule's
    grandfather (`current_id`), never as a selection."""
    current_id = credential_id if credential_id == user.git_credential_id else None
    stmt = git_identity_select(principal_for_user(session, user), current_id=current_id)
    return session.scalars(stmt.where(Credential.id == credential_id)).first()


def _require_git_type(credential: Credential, *, who: str, forge: str) -> None:
    """Refuse a credential that cannot authenticate a git remote.

    Separate from the dropdown filter: `resolve_for_user`'s `credential_id` is any id its caller names.
    """
    if credential.type not in GIT_CREDENTIAL_TYPES:
        label = FORGE_LABELS[forge]
        raise GitAccessError(
            f"{who}'s Git Access names credential '{credential.name}', which is a "
            f"{type_label(credential.type)} credential and cannot authenticate a git remote. Pick a "
            f"{label} credential instead under {profile_path(forge)}."
        )


def git_login_for(session: Session, credential: Credential, repo: RepoRef) -> GitLogin:
    """The login a git client presents to `repo` with `credential`: the token (`token_for`, renewed when
    due), then the forge's on-use revocation check (`git_probe`, D-A14), then the username table
    (`_GIT_LOGINS`). The single home of which username each forge and token kind takes.

    `credential` must be one `resolve_*` returned for `repo`; the root is re-checked as a belt, since a
    token served to the wrong host is a leak. A revocation found here is recorded on the row and raised
    as `GitAccessNeedsReconnect`."""
    if credential_root(credential) != repo.root or credential.type != repo.forge:
        raise GitAccessError(
            f"Credential '{credential.name}' does not authenticate {repo.root}, so it is not sent there. "
            f"Choose a {FORGE_LABELS[repo.forge]} credential under {profile_path(repo.forge)}."
        )
    config = credential.config or {}
    if rest_username_problem(credential.type, config) is not None:
        raise GitAccessError(
            f"Credential '{credential.name}' is a {FORGE_LABELS[repo.forge]} API token with no Atlassian "
            f"account email, which the {FORGE_LABELS[repo.forge]} API requires beside it. Open "
            f"{profile_path(repo.forge)} and paste the token again with its account email, then try again."
        )
    token = token_for(session, credential)
    if git_probe.claim_check(credential.id):
        verdict = git_probe.probe(
            credential.type, repo.root, git_probe.tester_config(dict(credential.config or {}), token),
            deadline=git_probe.new_deadline(),
        )
        if verdict == git_probe.REVOKED and not git_probe.just_stored(credential.updated_at):
            git_probe.mark_revoked(credential.id)
            label = FORGE_LABELS[repo.forge]
            raise GitAccessNeedsReconnect(
                f"{label} reports that the token in credential '{credential.name}' for {repo.root} has "
                f"been revoked. Open {profile_path(repo.forge)} and reconnect or paste a new token, "
                "then try again."
            )
    return _login(credential, token)


def rest_login_for(session: Session, credential: Credential) -> GitLogin:
    """`git_login_for` for a REST read by the person themself (the repo lookup, `app/forge_repos.py`): the
    token renewed when due, without the on-use revocation probe — the read itself is the probe, and its
    failure is reported where it happens. Refuses a row missing its REST username, as `git_login_for` does."""
    if rest_username_problem(credential.type, credential.config or {}) is not None:
        raise GitAccessError(f"Credential '{credential.name}' has no account email for the REST API.")
    return _login(credential, token_for(session, credential))


def _login(credential: Credential, token: str) -> GitLogin:
    """The username table's answer for `credential` carrying `token`."""
    config = credential.config or {}
    kind = credential_kind(credential)
    scheme, username = _GIT_LOGINS[(credential.type, kind)]
    rest_key = _REST_USERNAME_FIELDS.get((credential.type, kind))
    api_username = str(config[rest_key]).strip() if rest_key else ""
    return GitLogin(scheme=scheme, username=username, token=token, api_username=api_username)


def resolve_for_worktree_clone(
    session: Session, node_id: UUID, project_id: int, repo: Optional[RepoRef] = None
) -> Credential:
    """The credential a pool clone of `project_id` on `node_id` authenticates with: the connection, for
    that project's forge root — or `repo`'s, one of its connected repositories — of the person
    who declared that project's slots on that node (a dynamic slot is declared as its task's starter,
    `slot_allocation._declarer`).

    The earliest human declarer wins (`created_at`, then `id` for ties), which is stable as slots are
    added but not under deletion of the earliest slot. System-actor declarers are skipped. It refuses
    rather than trying the next declarer, so the clone never runs as someone who did not ask, and the
    refusal names the person to fix.
    """
    retry = CLONE_RETRY
    if repo is None:
        project = session.scalars(select(Project).where(Project.id == project_id)).first()
        repo = project_repo(project, retry=retry)
    declarer_id = clone_owner_id(session, node_id, project_id)
    if declarer_id is None:
        label = FORGE_LABELS[repo.forge]
        outsider = _declarer_outside_org(session, node_id, project_id)
        if outsider is not None:
            # An operator declared from outside the org: the org cannot lend their connection, and
            # "no person declared" would send the reader looking for a declaration that is there.
            raise GitAccessError(
                f"This project's slots on this node were declared by {outsider}, who is not a member of this "
                f"organization, so its clone cannot use their {label} account. Open one of its slots on the "
                f"Worktrees page, choose Change clone owner, and pick a member who has connected {label} for "
                f"{repo.root} under {profile_path(repo.forge)}."
            )
        raise GitAccessError(
            f"No one who can read {repo.root} started a task on this node yet. "
            f"Ask the task's starter to connect {label} under {profile_path(repo.forge)}."
        )
    return resolve_for_user(session, declarer_id, repo, retry=retry)


def clone_owner_id(session: Session, node_id: UUID, project_id: int) -> Optional[UUID]:
    """The person a (node, project) pair's slots authenticate as: the earliest human declarer of its slots
    (`resolve_for_worktree_clone`; changed by `routes/pool_slots.py::set_clone_owner`). None: nobody."""
    return session.scalar(
        select(Worktree.created_by_id)
        .join(AdminUser, AdminUser.id == Worktree.created_by_id)
        .where(
            Worktree.node_id == node_id,
            Worktree.project_id == project_id,
            AdminUser.is_system.is_(False),
        )
        .order_by(Worktree.created_at.asc(), Worktree.id.asc())
        .limit(1)
    )


def _declarer_outside_org(session: Session, node_id: UUID, project_id: int) -> Optional[str]:
    """The email of the pair's earliest human declarer, for `resolve_for_worktree_clone` once its own org-scoped
    read found none: anyone found here declared from outside the org (an operator).

    The declarer ids come from `session` (rows this org owns, a declaration not yet committed included); only
    who they are is read fleet-wide. One email out, for the refusal that names them; None when nobody but the
    system declared."""
    # Deferred: `app.db` installs ORM listeners at import time; route modules import this one.
    from app.db import BridgedSession
    from app.tenancy import seam

    declarers = list(dict.fromkeys(session.scalars(
        select(Worktree.created_by_id)
        .where(Worktree.node_id == node_id, Worktree.project_id == project_id, Worktree.created_by_id.is_not(None))
        .order_by(Worktree.created_at.asc(), Worktree.id.asc())
    )))
    if not declarers:
        return None
    with seam.detached(), seam.operator_scope(), BridgedSession() as fleet:
        people = dict(fleet.execute(
            select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_(declarers), AdminUser.is_system.is_(False))
        ).tuples().all())
    return next((people[d] or str(d) for d in declarers if d in people), None)


#: The two GitHub apps: kind -> (operator knob, credential type, what the person is told it is).
#: `git_connect_app` names the kind new grants come from; `app_config` falls back to the other.
APP_KNOBS: dict[str, tuple[str, str, str]] = {
    "github_app": ("git_github_app_credential_id", "github_install_app", "GitHub App"),
    "oauth_app": ("git_app_credential_id", "github_app", "GitHub OAuth App"),
}
#: Each knob's label in Settings > Integrations > GitHub, for refusals that name it.
_APP_KNOB_LABELS = {"github_app": "GitHub App credential", "oauth_app": "OAuth App credential"}


class AppConfig(NamedTuple):
    """One operator-designated GitHub app, read out as plain scalars.

    Scalars, not the ORM row: `app_config` reads on its own session, closed before return, so a row
    would be detached. `id` and `name` are for logs and refusals, never a second lookup. `kind` says which
    app it is; `slug` is the GitHub App's install-page name, blank for the OAuth App.
    """

    id: UUID
    name: str
    client_id: str
    client_secret: str
    kind: str = "oauth_app"
    slug: str = ""

    def __repr__(self) -> str:
        # Never the client secret: a stray `log.info("%s", app)` must not leak it.
        return f"AppConfig(id={self.id}, name={self.name!r}, kind={self.kind}, client_id={self.client_id!r})"


def chosen_app_kind() -> str:
    """The kind `git_connect_app` names (a closed choice; anything else reads as the default GitHub App)."""
    raw = settings_reads.get_str("git_connect_app").strip()
    return raw if raw in APP_KNOBS else "github_app"


def known_app_kind(raw: Optional[str]) -> Optional[str]:
    """`raw` when it names a kind (`APP_KNOBS`), else None ("the one to use now", `app_config`): for a kind
    carried through a flow cookie, which may predate it or be blank."""
    return raw if raw in APP_KNOBS else None


def token_app_kind(access_token: str) -> str:
    """Which app minted a stored GitHub token: a GitHub App's user token is `ghu_`, anything else the OAuth
    App's. Only the prefix is examined."""
    return "github_app" if github_oauth.is_app_issued(access_token) else "oauth_app"


def minted_by(app: AppConfig, access_token: str) -> bool:
    """Whether `access_token` is the kind `app` mints. False means the credential designated as one kind holds
    the other kind's client pair (an OAuth App's id in the GitHub App setting, or the reverse): the caller
    refuses the grant and gives it back."""
    return token_app_kind(access_token) == app.kind


def app_config(*, kind: Optional[str] = None, open_client_secret: bool = True) -> AppConfig:
    """The GitHub app Connect GitHub and GitHub sign-in authorize against: one per kind per deployment, no
    org.

    `kind=None` is "the one to use now": `chosen_app_kind()`, else the other kind when the chosen one is not
    designated, so a deployment with one app needs no choice made; the refusal, when neither is designated,
    names the chosen one. A named `kind` reads that app only — for a token that app minted (renewal, revoke).

    A no-grant read of the one credential named by a setting; it authenticates no git remote. It runs
    under `seam.detached()` + `seam.operator_scope()` on its own session, because `operator_scope()`
    refuses to widen an org-bound context and a session keeps its org stamp for life. Entitlement:
    nothing is caller-selectable; every caller reads the same operator-written settings and rows.

    The operator-plane read applies no org filter, so the row's plane is asserted explicitly, before any
    refusal that names the credential: credential names can carry tenant identity and reach ordinary
    users as a 503 body. The write-door check in `operator_settings` does not cover pointers stored
    earlier, so it is asserted at use too.

    `open_client_secret=False` answers only "is it usable" (the Git Access page's Connect offer) without
    opening the secret: `client_secret` is then blank and must not be sent anywhere.
    """
    # Deferred: `app.db` installs ORM listeners at import time; route modules import this one.
    from app.db import BridgedSession
    from app.tenancy import seam

    if kind is None:
        chosen = chosen_app_kind()
        other = "oauth_app" if chosen == "github_app" else "github_app"
        if not settings_reads.get_str(APP_KNOBS[chosen][0]).strip() and \
                settings_reads.get_str(APP_KNOBS[other][0]).strip():
            chosen = other
        kind = chosen
    with seam.detached(), seam.operator_scope(), BridgedSession() as session:
        credential = _designated_app(session, kind)
        # Read out as plain strings before the session closes (see `AppConfig`). The client secret is a
        # sealed field, opened through the one seam (`credential_crypto.open_secret`) because it is
        # consumed here; `client_id` and `slug` are plain.
        config = credential.config or {}
        what = APP_KNOBS[kind][2]
        return AppConfig(
            id=credential.id,
            name=credential.name,
            client_id=str(config.get("client_id") or ""),
            client_secret=(
                _opened_or_git_error(credential, "client_secret", f"the {what}'s client secret")
                if open_client_secret else ""
            ),
            kind=kind,
            slug=str(config.get("slug") or "") if kind == "github_app" else "",
        )


def _designated_app(session: Session, kind: str) -> Credential:
    """The row `kind`'s knob names (`APP_KNOBS`), asserted to be operator-plane and of that kind's type.

    `session` must be an operator-scoped one of the caller's own (`app_config`'s docstring says why). The
    plane is asserted before any refusal that names the credential: credential names can carry tenant
    identity and reach ordinary users as a 503 body.
    """
    knob, credential_type, what = APP_KNOBS[kind]
    setting = _APP_KNOB_LABELS[kind]
    raw = settings_reads.get_str(knob).strip()
    if not raw:
        raise GitAccessError(
            f"No {what} is configured, so GitHub accounts cannot be connected or sign in. Create "
            f"a '{type_label(credential_type)}' credential holding the {what}'s Client ID and Client "
            f"secret, then name its id in '{setting}' under Settings > Integrations > GitHub."
        )
    credential = _credential_by_setting(session, raw, setting=setting)
    if credential.org_id not in OPERATOR_PLANE_ORG_IDS:
        # Names nothing about the row (see docstring). `org_id` is NOT NULL, so a None falls
        # into this fail-closed arm.
        log.warning(
            "git-access: '%s' names credential id=%s owned by org=%s, which is "
            "not the operator plane — refused", setting, credential.id, credential.org_id,
        )
        raise GitAccessError(
            f"'{setting}' in Settings > Integrations > GitHub names a credential that does not belong "
            f"to this deployment, so it cannot be used. Re-pick the {what} in that setting."
        )
    if credential.type != credential_type:
        raise GitAccessError(
            f"'{setting}' names credential '{credential.name}', which is a "
            f"{type_label(credential.type)} credential rather than a {what}. Point it at a "
            f"'{type_label(credential_type)}' credential."
        )
    return credential


def _opened_or_git_error(credential: Credential, field: str, what: str) -> str:
    """`field` opened through the one seam, `""` when the row holds no such secret, or a `GitAccessError`
    when the stored envelope cannot be opened (moved, tampered, or its row's URL edited since the seal):
    a reader of this module gets a reason naming the row to re-enter, never a 500."""
    if not has_sealed_secret(credential.config or {}, field):
        return ""
    try:
        return open_secret(credential, field)
    except ValueError:
        raise GitAccessError(
            f"Credential '{credential.name}' holds {what} that can no longer be opened. Re-enter it "
            "under Directory > Secrets."
        ) from None


def not_configured_sentence(forge: str) -> str:
    """The operator-facing refusal when `forge`'s Connect app is not designated: the credential type to
    create and the setting to fill, in `app_config`'s shape. Single home for Connect's 409 and the Git
    Access page's `connect.reason` (`routes/forge_connect.py`)."""
    spec = forge_oauth.FORGE_OAUTH[forge]
    app_type = type_label(spec.app_type)
    article = "an" if app_type[:1].lower() in "aeiou" else "a"
    return (
        f"No {spec.label} app is configured, so {spec.label} accounts cannot be connected. Create "
        f"{article} '{app_type}' credential holding {spec.app_holds}, then name its id in 'App "
        f"credential' under Settings > Integrations > {spec.settings_section}."
    )


def forge_app_config(forge: str, *, open_client_secret: bool = True) -> forge_oauth.ForgeApp:
    """The operator-designated Connect app for a non-GitHub `forge` (`app_config`'s pattern exactly: the
    setting, its own detached operator-scope session, the plane assertion before any refusal quoting the
    row, the type assertion, the sealed secret opened through the one seam).

    `open_client_secret=False` answers only "is it usable" (the Git Access page) without opening the
    secret; the returned `client_secret` is then blank and must not be sent anywhere."""
    from app.db import BridgedSession
    from app.tenancy import seam

    spec = forge_oauth.FORGE_OAUTH[forge]
    if forge == "github":
        # GitHub's App has its own reader and wording (`app_config`); a caller bug to route it here.
        raise ValueError("forge_app_config serves the non-GitHub forges; use app_config for GitHub")
    raw = settings_reads.get_str(spec.app_setting).strip()
    if not raw:
        raise GitAccessError(not_configured_sentence(forge))
    with seam.detached(), seam.operator_scope(), BridgedSession() as session:
        credential = _credential_by_setting(session, raw, setting="App credential", section=spec.settings_section)
        if credential.org_id not in OPERATOR_PLANE_ORG_IDS:
            log.warning(
                "git-access: %s names credential id=%s owned by org=%s, which is not the operator "
                "plane — refused", spec.app_setting, credential.id, credential.org_id,
            )
            raise GitAccessError(
                f"'App credential' in Settings > Integrations > {spec.settings_section} names a credential "
                f"that does not belong to this deployment, so it cannot be used. Re-pick the {spec.label} "
                "app in that setting."
            )
        if credential.type != spec.app_type:
            raise GitAccessError(
                f"'App credential' in Settings > Integrations > {spec.settings_section} names credential "
                f"'{credential.name}', which is a {type_label(credential.type)} credential. Point it at a "
                f"'{type_label(spec.app_type)}' credential."
            )
        config = credential.config or {}
        client_id = str(config.get("client_id") or "").strip()
        # Only Entra has a tenant; every other forge's app reads as the default, unused.
        tenant = (
            forge_oauth.valid_tenant(str(config.get("tenant") or "")) if forge == "azure_devops"
            else forge_oauth.DEFAULT_TENANT
        )
        if not client_id or tenant is None:
            raise GitAccessError(
                f"Credential '{credential.name}' has no usable "
                f"{'tenant' if client_id else 'client id'}. Fix it under Directory > Secrets."
            )
        what = f"the {spec.label} app's client secret"
        if open_client_secret:
            secret = _opened_or_git_error(credential, "client_secret", what)
            if not secret:
                raise GitAccessError(
                    f"Credential '{credential.name}' holds no client secret. Re-enter it under Directory > "
                    "Secrets."
                )
        else:
            if not has_sealed_secret(config, "client_secret"):
                raise GitAccessError(
                    f"Credential '{credential.name}' holds no client secret. Re-enter it under Directory > "
                    "Secrets."
                )
            secret = ""
        return forge_oauth.ForgeApp(
            id=credential.id, name=credential.name, client_id=client_id, client_secret=secret,
            tenant=tenant,
        )


def operator_github_credential(credential_id: UUID):
    """The `credentials` row `credential_id` names, if it is a GitHub credential of the operator plane: the one
    statement behind the `compliance_github_credential_id` knob, so a tenant's credential id pasted into it
    never resolves."""
    return select(Credential).where(
        Credential.id == credential_id, Credential.type == "github", Credential.org_id.in_(OPERATOR_PLANE_ORG_IDS),
    )


def _credential_by_setting(
    session: Session, raw: str, *, setting: str, section: str = "GitHub"
) -> Credential:
    """One credential named by an operator setting holding its UUID.

    A malformed id and a missing row get different messages (bad paste vs deleted row); both name the setting.
    """
    try:
        credential_id = UUID(raw)
    except ValueError as exc:
        raise GitAccessError(
            # The setting renders a credential picker and the write refuses malformed ids, so this is
            # reachable only for old stored values or direct DB writes.
            f"'{setting}' in Settings > Integrations > {section} is not a valid credential id. Re-pick the credential "
            "in that setting."
        ) from exc
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise GitAccessError(
            f"'{setting}' in Settings > Integrations > {section} names credential {credential_id}, which no longer "
            "exists. Point it at a current credential."
        )
    return credential


def token_for(session: Session, credential: Credential) -> str:
    """The token to send for `credential`, refreshing it first when it is expiring and can be.

    PATs and Apps with expiry off are returned as stored; an App token near expiry is refreshed. This
    function decides whether to refresh and takes a `FOR UPDATE` lock with a re-read so a concurrent
    caller that already refreshed is seen; the rotation itself runs in `app/durable/git_token.py`, whose
    deterministic id admits one rotation per grant across replicas (the race loser adopts the winner's
    outcome). The lock is released (committed) before the workflow runs, since its persist step updates
    this row from its own session and would otherwise self-deadlock. A rotation fenced off because the
    row moved on (e.g. a reconnect) is a success: the current row's token is served. Refusals:
    `GitAccessNeedsReconnect` for a dead refresh token; `GitAccessError` otherwise, which is retried on the
    next use.
    """
    if not _needs_refresh(credential):
        return _stored_token(credential)

    # `populate_existing=True` is load-bearing: `credential` is already in the identity map, and without
    # it the locked read returns the stale pre-lock instance, silently skipping the double-check.
    # `select().with_for_update()` and `session.get(..., with_for_update=True)` alone both stay stale.
    locked = session.get(
        Credential, credential.id, with_for_update=True, populate_existing=True
    )
    if locked is None:
        # Deleted between resolution and the lock.
        raise GitAccessError(
            f"Credential '{credential.name}' was deleted while it was being used. Choose another "
            "under Profile > Git."
        )
    # Re-read inside the lock: a concurrent caller may have refreshed, and refreshing again would
    # invalidate its token.
    if not _needs_refresh(locked):
        return _stored_token(locked)

    # Pre-flight: refuse our own misconfiguration (no App, deleted or wrong-type row) here, before a
    # workflow id is claimed, because DBOS caches a raised error for a deterministic id (`run_refresh` gets
    # past it only one retry window later).
    # The result is discarded; `rotate` resolves the App itself so no secret is a checkpointed argument.
    # It uses its own session, and this transaction holds only a row lock. Keyed by forge, like the
    # rotation itself (`REFRESHABLE_TYPES`).
    _REFRESH_PREFLIGHT[locked.type]()

    # The grant's identity (a timestamp, never a token), so the workflow id names one grant.
    grant_expires_at_iso = str(locked.config.get("token_expires_at") or "")
    credential_id = str(locked.id)
    # Read before commit, which expires loaded attributes.
    credential_name = locked.name
    credential_type = locked.type

    # Releases the lock; holding it across the workflow would deadlock (see docstring).
    session.commit()

    # Deferred: importing `app/durable/` registers DBOS workflows as a side effect.
    from app.durable import git_token

    # No guard: an unreachable engine must be a refusal, not an expiring token or an inline refresh.
    outcome = git_token.run_refresh(credential_id, grant_expires_at_iso)

    code = outcome.get("code")
    label = FORGE_LABELS.get(credential_type, type_label(credential_type))
    # `GRANT_SUPERSEDED` is not a refusal: the row holds a token somebody else just installed, so it
    # falls through to the re-read.
    if not outcome.get("ok") and code != git_token.GRANT_SUPERSEDED:
        if outcome.get("dead") or code in forge_oauth.FORGE_OAUTH[credential_type].refresh_dead_codes:
            raise GitAccessNeedsReconnect(
                f"The {label} connection behind credential '{credential_name}' has expired and cannot be "
                f"renewed. Open Profile > Git > {label} and connect {label} again."
            )
        if code == git_token.CREDENTIAL_GONE:
            # Deleted between the lock release and the rotation.
            raise GitAccessError(
                f"Credential '{credential_name}' was deleted while it was being used. Choose another "
                "under Profile > Git."
            )
        # Bounded: `detail` relays the forge's text into journald and runner terminals.
        detail = str(outcome.get("detail") or "")
        if len(detail) > 200:
            detail = detail[:200] + "…"
        # Everything reaching here is retryable, so the message says the next use retries, with
        # reconnecting only as the fallback.
        raise GitAccessError(
            f"Could not renew the {label} token for credential '{credential_name}': {detail} — the next "
            "use of this credential tries the renewal again, so this is usually transient. If it keeps "
            f"failing, open Profile > Git > {label} and connect {label} again."
        )

    # Re-read with `populate_existing`: the persist step committed on another session.
    refreshed = session.get(Credential, credential.id, populate_existing=True)
    if refreshed is None:
        # A fenced write and a deleted row look the same from the workflow; this re-read tells them apart.
        raise GitAccessError(
            f"Credential '{credential_name}' was deleted while it was being used. Choose another "
            "under Profile > Git."
        )
    if code == git_token.GRANT_SUPERSEDED:
        # Logged separately so an operator sees their reconnect stood.
        log.info(
            "git-access: credential id=%s was replaced while our rotation was in flight — serving the "
            "grant now on the row (expires_at=%s)",
            credential_id, refreshed.config.get("token_expires_at"),
        )
    else:
        log.info(
            "git-access: refreshed credential id=%s type=%s expires_at=%s",
            credential_id, credential_type, refreshed.config.get("token_expires_at"),
        )
    return _stored_token(refreshed)


def renew_if_expiring(session: Session, credential: Credential) -> None:
    """Renew `credential`'s forge grant in place when it is expiring and renewable (`token_for`), else
    nothing: for a reader about to hand the whole row out (the runner's `credential.get`), which must not
    serve a token the control plane would have renewed on a git use. A row that needs no renewal is never
    opened here, so a NodeVault field the control plane cannot open stays untouched. Raises what `token_for`
    raises."""
    if _needs_refresh(credential):
        token_for(session, credential)
        # Ends the row lock `token_for` keeps when a concurrent caller had already renewed it.
        session.commit()


def is_connect_grant(credential: Credential) -> bool:
    """Whether `credential` is a grant a forge Connect minted, which the control plane renews (`token_for`): its
    refresh token is the control plane's alone, since a forge's refresh token is single-use."""
    return (
        credential.type in REFRESHABLE_TYPES
        and credential_root(credential) == forge_oauth.FORGE_OAUTH[credential.type].root
    )


def readable_token(credential: Credential) -> Optional[str]:
    """The row's stored token as it stands (no renewal), or None when it holds none or it
    cannot be opened: for a writer about to replace it (`git_connections.connect_github`). Never log it."""
    try:
        return _stored_token(credential)
    except GitAccessError:
        return None


def _needs_refresh(credential: Credential) -> bool:
    """True only for a row that both expires soon and holds a refresh token.

    Both conjuncts are needed: without the token check, an expiry-off App row with a stale expiry would
    fail a refresh; without the expiry check, every read would spend a rotating token. An unparseable
    `token_expires_at` reads as not expiring. The refresh token is only tested for presence (a sealed
    marker is truthy), never opened: the rotation reads it (`durable/git_token.py`).
    """
    if not is_connect_grant(credential):
        # A self-managed GitLab row: no Connect minted it (D4), so no rotation may spend its token.
        return False
    if not credential.config.get("refresh_token"):
        return False
    expires_at = _parsed_expiry(credential.config.get("token_expires_at"))
    if expires_at is None:
        return False
    return utcnow() + _REFRESH_MARGIN >= expires_at


def _parsed_expiry(raw: object) -> Optional[datetime]:
    """`raw` as a timezone-aware UTC instant, or None when it is absent or unreadable.

    A naive timestamp is read as UTC (only a hand edit produces one).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return as_utc(parsed)


def _stored_token(credential: Credential) -> str:
    """The `token` field opened through the one seam (`credential_crypto.open_secret`, the private
    operation, since the token is consumed here), or a `GitAccessError` naming the row when it is empty
    (hand edit or a half-written OAuth callback)."""
    token = _opened_or_git_error(credential, "token", "a token")
    if not token:
        raise GitAccessError(
            f"Credential '{credential.name}' holds no token. Re-enter it under Directory > Secrets, "
            "or connect again from Profile > Git."
        )
    return token


def _store_grant(credential: Credential, grant: github_oauth.TokenGrant) -> None:
    """Write a fresh grant onto `credential`, replacing all three OAuth fields together.

    Absent refresh token/expiry are cleared, so an expiry-off grant is never treated as renewable, and a
    recorded revocation (`grant_dead_at`) is cleared: a fresh grant is a reconnect. The dict is
    reassigned, not mutated: JSONB is tracked by identity and a mutation emits no UPDATE.
    """
    config = dict(credential.config or {})
    config.pop("grant_dead_at", None)
    config["token"] = grant.access_token
    if grant.refresh_token:
        config["refresh_token"] = grant.refresh_token
    else:
        config.pop("refresh_token", None)
    if grant.expires_at:
        config["token_expires_at"] = grant.expires_at.isoformat()
    else:
        config.pop("token_expires_at", None)
    credential.config = config


def store_grant(credential: Credential, grant: github_oauth.TokenGrant) -> None:
    """`_store_grant`, exported for `git_connections.connect_github` (first grant) and
    `app/durable/git_token.py::persist` (rotations), so both write the fields identically.
    """
    _store_grant(credential, grant)


#: What a rotation of each `REFRESHABLE_TYPES` forge needs configured, checked before a workflow id is
#: claimed (`token_for`). Keys held equal to `REFRESHABLE_TYPES` by a test.
_REFRESH_PREFLIGHT = {
    # Only a GitHub App's token carries a refresh token; a classic OAuth App's never expires.
    "github": partial(app_config, kind="github_app"),
    **{forge: partial(forge_app_config, forge) for forge in forge_oauth.FORGE_OAUTH if forge != "github"},
}
