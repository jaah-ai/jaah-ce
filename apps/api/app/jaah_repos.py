"""Repositories Jaah made for a project in its own GitHub org (`models.jaah_repos.JaahRepo`).

Every project created without a repository of its own gets one (`routes/projects.py::_create_project_in`, `intake`):
a web app's comes from its Easy Mode template (`hosted_apps/service.py::create`), any other a plain private repo
(`hosted_apps/github.py::create_plain_repo`). Its runners push with a one-repo login the console's GitHub App mints
(`easy_mode_runner_tokens`), served by `routes/agent/task_secrets.py::_jaah_repo_token_or_none` for a repo
`is_jaah_repo` says is the project's own; `routes/projects.py::_refuse_jaah_repo_edit` keeps the project from
re-pointing at another repo of that org.

THE ORG PREFIX. Every such repo is named `<org slug>-<name>` (`repo_prefix`): `acme-app-shop`, `acme-my-project`. It
is the mechanical separation of tenants inside the one GitHub org: a git login for a repo of that org without the
asking project's org prefix is refused (`require_org_prefix`, at `routes/agent/task_secrets.py`), and the node
refuses it again on its own (`jaah_repo_prefix` rides the git login and the project listing to the node's credential
helper and pre-push guard). An org slug is letters and digits only (`app/org_slug.py::ORG_SLUG_RE`), so one org's
prefix is never another's. A slug rename renames the org's repos with it (`app/durable/jaah_repo_prefix.py`).
"""
from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import unquote
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.actors import stamp_actor
from app.authz.principal import Principal
from app.forges import RepoRef
from app.models.identity import AdminUser
from app.models.jaah_repos import JAAH_REPO_STATES, JaahRepo
from app.models.projects import Project
from app.tenancy.ids import is_operator_user

#: States whose repo still serves a runner login: `active` a write one, the two moving states a read-only one.
SERVED_STATES = ("active", "migrating", "move_failed")
WRITE_PERMISSIONS: dict[str, str] = {
    "contents": "write", "pull_requests": "write", "workflows": "write", "metadata": "read",
}
READ_PERMISSIONS: dict[str, str] = {"contents": "read", "metadata": "read"}
#: Added to a write login wherever the App's installation grants them, at the level it grants (never a refused mint):
#: a PR's check rollup, which every `jaah-forge pr view|list|checks` reads over GraphQL and GitHub refuses ("Resource
#: not accessible by integration") to a token without them, and the merge lane's `merge-lane` commit status.
CHECK_PERMISSIONS: dict[str, str] = {"checks": "read", "statuses": "write"}
#: Added the same way: what the rest of a runner's CI verbs use on its one repository
#: (`apps/jaahd/forge_cli/adapters/github.py`): Actions runs, logs, rerun and dispatch (`ci`), and Actions variables and
#: secrets at the repository or an environment (`var|secret`). Not Dependabot secrets: the App does not ask for them.
CI_PERMISSIONS: dict[str, str] = {
    "actions": "write", "secrets": "write", "actions_variables": "write", "environments": "write",
}


def github_org() -> str:
    """The org Jaah's repositories live in (the operator knob `easy_mode_github_org`); blank matches no repo."""
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_text("easy_mode_github_org").strip()


def repo_prefix(org_slug: str) -> str:
    """The name prefix of every repository Jaah makes for the org whose slug is `org_slug`."""
    return f"{org_slug}-"


def org_slug_of(session: Session, org_id: UUID) -> str:
    from app.models.orgs import Org

    return session.scalar(select(Org.slug).where(Org.id == org_id)) or ""


def jaah_repo_prefix(org_slug: str) -> str:
    """`<github org>/<org slug>-`: what every path of the org's repos in the Jaah org starts with — the wire field the
    node's checks read (`apps/jaahd/git_credential_helper.py::jaah_prefix_refusal`); "" when no Jaah org is set."""
    org = github_org()
    return f"{org}/{repo_prefix(org_slug)}" if org and org_slug else ""


def require_org_prefix(session: Session, org_id: UUID, repo: Optional[RepoRef]) -> None:
    """`GitAccessError` when `repo` is in the Jaah org but not named with org `org_id`'s prefix — never a fall-through
    to a person's login: no login of any kind is served for another org's repository there."""
    from app.git_access import GitAccessError

    if not in_jaah_org(repo):
        return
    prefix = jaah_repo_prefix(org_slug_of(session, org_id))
    if not prefix or not unquote(repo.path).lower().startswith(prefix.lower()):  # type: ignore[union-attr]
        raise GitAccessError(f"{repo.path} is not one of this organization's repositories "  # type: ignore[union-attr]
                             f"(its repositories in {github_org()} are named {prefix.split('/', 1)[-1]}…).")


def in_jaah_org(repo: Optional[RepoRef]) -> bool:
    """Whether `repo` is a github.com repository owned by `github_org()`."""
    if repo is None or repo.forge != "github" or repo.root != "github.com":
        return False
    # Decoded: `jaah%2Dapps` is the same owner to GitHub, and must not read as another.
    owner, _, name = unquote(repo.path).partition("/")
    org = github_org()
    return bool(name) and bool(org) and owner.lower() == org.lower()


def row_for(session: Session, project_id: int) -> Optional[JaahRepo]:
    """The project's Jaah repo that is not `archived` (at most one), or None."""
    return session.scalar(
        select(JaahRepo).where(JaahRepo.project_id == project_id, JaahRepo.state != "archived")
    )


#: The refusal a member reads for a setting of a project on a Jaah repo (`refuse_member_edit`).
MANAGED_REFUSAL = "Jaah manages this project's repository and settings. Ask Jaah support to change it."


def managed_ids(session: Session, project_ids: Iterable[int]) -> frozenset[int]:
    """THE JAAH-MANAGED PROJECTS among `project_ids`: those on an `active` Jaah repo — one in Jaah's GitHub org, which
    Jaah also hosts for a web app. A moving (`migrating`/`move_failed`), retiring or archived row (a hand-over archives
    it) is the project's own again. One query, for a list."""
    ids = list(project_ids)
    if not ids:
        return frozenset()
    return frozenset(session.scalars(
        select(JaahRepo.project_id).where(JaahRepo.project_id.in_(ids), JaahRepo.state == "active")
    ))


def managed(session: Session, project_id: int) -> bool:
    """Whether the project is Jaah-managed (`managed_ids`)."""
    return project_id in managed_ids(session, [project_id])


def operator_caller(actor: Optional[AdminUser], principal: Optional[Principal]) -> bool:
    """Whether the caller is Jaah's operator: on the operator plane, or an operator's tab acting as an org
    (`Principal.acting`). A node token (`principal is None`, a runner Jaah dispatched) counts too."""
    if principal is None:
        return True
    return principal.is_operator or principal.acting or (actor is not None and is_operator_user(actor))


def refuse_member_edit(session: Session, project_id: int, actor: Optional[AdminUser],
                       principal: Optional[Principal]) -> None:
    """403 when an org member writes a setting of a Jaah-managed project: only its Name, Default agent, Default persona,
    Agent instructions, Access, Budget and the move to a repo of its own stay theirs (`routes/projects.py::
    _refuse_managed_fields` for the project row). The operator passes (`operator_caller`)."""
    if not operator_caller(actor, principal) and managed(session, project_id):
        raise HTTPException(status_code=403, detail=MANAGED_REFUSAL)


def is_jaah_repo(session: Session, project: Optional[Project], repo: RepoRef) -> Optional[JaahRepo]:
    """`project`'s Jaah repo row when `repo` is it — in the Jaah org, named by that row, in a `SERVED_STATES` state —
    else None (the caller resolves a person's login as before)."""
    if project is None or not in_jaah_org(repo):
        return None
    row = row_for(session, project.id)
    if row is None or row.state not in SERVED_STATES or row.name.lower() != repo.path.lower():
        return None
    return row


def runner_app_repo(session: Session, project: Optional[Project], repo: RepoRef) -> Optional[JaahRepo]:
    """`project`'s Jaah repo row when `repo`'s git login is the console's GitHub App's, not a person's: the edition
    serves that login (`customers`) and `is_jaah_repo` says yes; else None. The one home of that rule: the runner
    token route (`routes/agent/task_secrets.py::_jaah_repo_token_or_none`) serves the app's login on it, and since a
    person's connection then decides nothing about the repo's runs or clones, the dispatch hold
    (`git_access.revoked_holds`) and the project page's preflight (`routes/projects.py::project_git_reach`) skip it."""
    from app import edition_features

    return is_jaah_repo(session, project, repo) if edition_features.enabled("customers") else None


def login_for(row: JaahRepo, repo: RepoRef):
    """The runner's one-repo git login for `row`'s repo (`easy_mode_runner_tokens.login_for`), bound to the repo's
    immutable id, never its name: a name a repo left behind (handed over, renamed) may be another org's repo now.
    `GitAccessError` while the id is not recorded yet (a web app still being provisioned)."""
    from app import easy_mode_runner_tokens
    from app.git_access import GitAccessError

    if row.repo_id is None:
        raise GitAccessError("This project's repository is still being set up, so it has no git login yet. "
                             "Try again in a few minutes.")
    optional = {**CHECK_PERMISSIONS, **CI_PERMISSIONS} if row.state == "active" else None
    return easy_mode_runner_tokens.login_for(repo, repo_id=row.repo_id, permissions=permissions_for(row),
                                             optional=optional)


def permissions_for(row: JaahRepo) -> dict[str, str]:
    return WRITE_PERMISSIONS if row.state == "active" else READ_PERMISSIONS


def name_taken(session: Session, full_name: str) -> bool:
    """Whether any row of this org names `full_name` (a retried create never adopts another project's repo)."""
    return session.scalar(
        select(func.count()).select_from(JaahRepo).where(func.lower(JaahRepo.name) == full_name.lower())
    ) != 0


def record(
    session: Session, *, org_id: UUID, project_id: int, name: str, kind: str, integration_branch: str,
    actor: AdminUser, repo_id: Optional[int] = None,
) -> JaahRepo:
    """An `active` row for a just-made repo, flushed in the caller's transaction."""
    row = JaahRepo(
        org_id=org_id, project_id=project_id, name=name, description="", kind=kind, state="active",
        integration_branch=integration_branch, repo_id=repo_id,
    )
    stamp_actor(row, actor, created=True)
    session.add(row)
    session.flush()
    return row


def move_to(row: JaahRepo, state: str) -> None:
    """The one writer of a row's move-or-retire state (`app/jaah_repo_replace.py`, `app/jaah_repo_retire.py`);
    `archived` stamps `archived_at`."""
    if state not in JAAH_REPO_STATES:
        raise ValueError(f"unknown jaah_repos state {state!r}")
    row.state = state
    if state == "archived":
        row.archived_at = func.now()


def archive(session: Session, project_id: Optional[int], actor: AdminUser) -> None:
    """The project's row `archived`, when it has one: its repo is no longer Jaah's (an app handed over)."""
    if project_id is None:
        return
    row = row_for(session, project_id)
    if row is not None:
        row.state = "archived"
        row.archived_at = func.now()
        stamp_actor(row, actor)


def follow(session: Session, project_id: Optional[int], full_name: str, repo_id: Optional[int]) -> None:
    """The project's row follows a web app's repo that had to take the next free name (`durable/hosted_app.py`)."""
    if project_id is None:
        return
    row = row_for(session, project_id)
    if row is not None:
        row.name = full_name
        row.repo_id = repo_id


def prefixed_rename(name: str, org_slug: str, old_slugs: list[str]) -> Optional[str]:
    """The `owner/repo` that `name` (`owner/repo`) takes under org `org_slug`'s prefix: None when it already has it;
    else any leading `repo_prefix` of one of `old_slugs` dropped, then the new prefix put in front. Pure."""
    owner, _, repo = name.rpartition("/")
    prefix = repo_prefix(org_slug)
    if repo.lower().startswith(prefix.lower()):
        return None
    for old in old_slugs:
        stale = repo_prefix(old)
        if old and repo.lower().startswith(stale.lower()) and len(repo) > len(stale):
            repo = repo[len(stale):]
            break
    return f"{owner}/{prefix}{repo}" if owner else f"{prefix}{repo}"


def follow_rename(session: Session, row: JaahRepo, new_full_name: str) -> None:
    """Everything that names `row`'s repo follows its rename to `new_full_name`: the row, the project's web app
    (`HostedApp.repo_full_name`), the project's primary repo URL and its setup answer's "Repository:" line
    (`project_intake.made_by_jaah`). Flushed in the caller's transaction; the caller commits."""
    from app.models.hosted_apps import HostedApp

    old = row.name
    row.name = new_full_name
    if row.project_id is None:
        return
    app = session.scalar(select(HostedApp).where(
        HostedApp.project_id == row.project_id, HostedApp.org_id == row.org_id,
        func.lower(HostedApp.repo_full_name) == old.lower(),
    ))
    if app is not None:
        app.repo_full_name = new_full_name
    repoint_project(session, row.project_id, row.org_id, old, new_full_name)


def repoint_project(session: Session, project_id: int, org_id: UUID, old_full_name: str, new_full_name: str) -> None:
    """The project's primary repo URL and its setup answer's "Repository:" line (`project_intake.made_by_jaah`)
    follow its Jaah-made repo from `old_full_name` to `new_full_name` (a rename, or a create that had to take the
    next free name — `durable/hosted_app.py`)."""
    from app import project_repos
    from app.project_intake import made_by_jaah

    project = session.scalar(select(Project).where(Project.id == project_id, Project.org_id == org_id))
    if project is None:
        return
    project_repos.upsert_primary(session, project, {"repo_url": f"https://github.com/{new_full_name}"}, None)
    if project.agent_instructions:
        project.agent_instructions = project.agent_instructions.replace(
            made_by_jaah(old_full_name), made_by_jaah(new_full_name))
