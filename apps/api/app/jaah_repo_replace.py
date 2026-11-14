"""Replacing a project's Jaah-made repository (`app/jaah_repos.py`) with one of its own: the save's choice and the
`repo_move` task.

`PUT /api/projects/{id}` (`routes/projects.py::apply_project_update`) asks `detect` before writing anything. A save
that replaces the primary while it is the project's `active` Jaah repo, with a repository outside Jaah's GitHub org,
or removes it, needs `jaah_repo` — absent, a 409 whose `detail` is `conflict_detail` and nothing is written:

* `retire`: the repositories are saved as sent and the row goes `retiring`; `app/jaah_repo_retire.py` archives the
  repo and removes any hosted app.
* `move` (only with `offer_move`: the project has a task done): the old repo stays connected as a non-primary,
  read-only repository; the row goes `migrating` with `source_head_sha` (the old integration branch's head); one
  `repo_move` task is filed and started as the saver (`file_move_task`). `app/jaah_repo_retire.py` retires the old
  repo once that task is done and the head is in the new repository, else marks the row `move_failed` and archives
  nothing.

Removing a `move_failed` row's repository (now a non-primary one) asks the same, `reason: "remove"`, retire only.
Easy Mode's view-only console (`Project.easy_mode`) ends with either answer: the project is on a repo of its own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app import edition_features, forges, jaah_repos, project_repos, task_lifecycle
from app.actors import stamp_actor
from app.authz.catalog import permission_key
from app.authz.principal import Principal
from app.authz.visibility import has_project_key
from app.models.identity import AdminUser
from app.models.jaah_repos import JaahRepo
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.persona_registry import SYSTEM_PERSONAS

log = logging.getLogger(__name__)

MOVE = "move"
RETIRE = "retire"
CHOICES = (MOVE, RETIRE)
REPO_MOVE_PERSONA = "repo_move"
#: The 409's `detail.code`, which the console's dialog keys on.
CONFLICT_CODE = "jaah_repo_replaced"
CONFLICT_MESSAGE = "Choose what happens to the Jaah repository."
NO_MOVE_OFFERED = "Moving the code is offered only once this project has a finished task. Choose retire."
APP_DELETE_NEEDS_PERMISSION = ("Replacing this repository deletes the project's hosted app, which needs permission to "
                               "delete this project.")
RUNNER_MAY_NOT_RETIRE = ("Retiring the Jaah repository archives the project's code, and while its hosted app runs either "
                         "answer deletes the app: a person must choose it in the console. Report it as a blocker.")
SOURCE_UNREADABLE = "The Jaah repository's code could not be read just now, so nothing was saved. Try again shortly."


@dataclass(frozen=True)
class OldRepo:
    """The Jaah repo's `project_repos` row as it stood before the save (which may rewrite or delete it)."""

    repo_url: str
    forge: Optional[str]
    forge_base_url: Optional[str]
    integration_branch: str

    @classmethod
    def of(cls, repo: ProjectRepo) -> "OldRepo":
        return cls(repo.repo_url, repo.forge, repo.forge_base_url, repo.integration_branch)


@dataclass(frozen=True)
class Replacement:
    """A save that replaces or removes the project's Jaah repo: why, whether a move is offered, and its stored row."""

    row: JaahRepo
    reason: str  # "replace" | "remove"
    offer_move: bool
    old: OldRepo


def _names_row(row: JaahRepo, repo: ProjectRepo) -> bool:
    try:
        ref = forges.parse_repo_url(repo.repo_url, repo.forge, repo.forge_base_url)
    except forges.ForgeUrlError:
        return False
    return jaah_repos.in_jaah_org(ref) and ref.path.lower() == row.name.lower()


def _same(repo: ProjectRepo | OldRepo, url: str, forge: Optional[str], base: Optional[str]) -> bool:
    return bool(project_repos.repo_keys(url, forge, base)
                & project_repos.repo_keys(repo.repo_url, repo.forge, repo.forge_base_url))


def _target(sent: dict[str, Any], repo_specs: Optional[list[project_repos.RepoSpec]], project: Project
            ) -> tuple[bool, Optional[tuple[str, Optional[str], Optional[str]]]]:
    """`(changes, primary)`: whether the save names the primary at all, and the primary it leaves (None: none)."""
    if repo_specs is not None:
        primary = next((spec for spec in repo_specs if spec.is_primary), None)
        return True, (primary.repo_url, primary.forge, primary.forge_base_url) if primary is not None else None
    if "repo_url" in sent:
        url = (sent["repo_url"] or "").strip()
        if not url:
            return True, None
        return True, (url, sent.get("forge", project.forge), sent.get("forge_base_url", project.forge_base_url))
    return False, None


def has_done_task(session: Session, project_id: int) -> bool:
    """Whether the project has a task the person sees as finished: `done`, not hidden, not a system persona's."""
    return bool(session.scalar(select(exists().where(
        Task.project_id == project_id, Task.state == task_lifecycle.DONE, Task.hidden.is_(False),
        Task.persona.notin_(SYSTEM_PERSONAS),
    ))))


def detect(session: Session, project: Project, sent: dict[str, Any],
           repo_specs: Optional[list[project_repos.RepoSpec]]) -> Optional[Replacement]:
    """The replacement this save makes, or None. Read before any write; `_refuse_jaah_repo_edit` has already refused a
    move to another repository of Jaah's org."""
    row = jaah_repos.row_for(session, project.id)
    if row is None:
        return None
    # Locked to the commit: two saves answering at once must not both move it (two `repo_move` tasks).
    session.refresh(row, with_for_update=True)
    if row.state not in ("active", "move_failed"):
        return None
    old = next((repo for repo in project_repos.repos_of(session, project.id) if _names_row(row, repo)), None)
    if old is None:
        return None
    if row.state == "move_failed":
        # The old repo, left connected by a failed move: removing it is the one change that asks.
        if old.is_primary or repo_specs is None or any(
            spec.id == old.id or _same(old, spec.repo_url, spec.forge, spec.forge_base_url) for spec in repo_specs
        ):
            return None
        return Replacement(row=row, reason="remove", offer_move=False, old=OldRepo.of(old))
    if not old.is_primary:
        return None
    changes, primary = _target(sent, repo_specs, project)
    if not changes:
        return None
    if primary is None:
        return Replacement(row=row, reason="remove", offer_move=False, old=OldRepo.of(old))
    url, forge, base = primary
    if _same(old, url, forge, base):
        return None
    try:
        if jaah_repos.in_jaah_org(forges.parse_repo_url(url, forge, base)):
            return None
    except forges.ForgeUrlError:
        return None  # the shape check names it
    return Replacement(row=row, reason="replace", offer_move=has_done_task(session, project.id),
                       old=OldRepo.of(old))


def live_app(session: Session, project_id: Optional[int]):
    """The project's hosted app while it still runs (not deleted, not being deleted, not handed over), or None."""
    if not edition_features.enabled("easy_mode_hosting"):
        return None
    if project_id is None:
        return None
    from app.hosted_apps import service

    app = service.app_of_project(session, project_id)
    if app is None or app.deleted_at is not None or app.handed_over_to is not None or app.state == "deleting":
        return None
    return app


def _app_address(session: Session, project_id: Optional[int]) -> Optional[str]:
    if not edition_features.enabled("easy_mode_hosting"):
        return None
    app = live_app(session, project_id)
    if app is None:
        return None
    from app.hosted_apps import service

    return service.address_or_none(app)


def conflict_detail(session: Session, replacement: Replacement) -> dict[str, Any]:
    return {
        "code": CONFLICT_CODE, "message": CONFLICT_MESSAGE, "reason": replacement.reason,
        "offer_move": replacement.offer_move, "jaah_repo": replacement.row.name,
        "app_address": _app_address(session, replacement.row.project_id),
    }


def require_choice(session: Session, replacement: Optional[Replacement], choice: Optional[str],
                   confirm: Optional[str], principal: Optional[Principal], project_id: int) -> None:
    """409 when the save replaces the Jaah repo and names no choice; 422 for `move` where none is offered. A choice
    sent with no replacement is ignored (a dialog's resend).

    Either answer ends with the hosted app deleted, so while one runs the answer takes what deleting the project
    takes (`routes/projects.py::_easy_mode_app_to_delete`): `projects:delete` (403) and `jaah_repo_confirm` naming
    the app's address (422) — `app_address` in the 409 is that address."""
    if replacement is None:
        return
    if choice is None:
        raise HTTPException(status_code=409, detail=conflict_detail(session, replacement))
    if choice == MOVE and not replacement.offer_move:
        raise HTTPException(status_code=422, detail=NO_MOVE_OFFERED)
    if not edition_features.enabled("easy_mode_hosting"):
        return
    app = live_app(session, project_id)
    if app is None:
        return
    # A person's act: a caller with no principal (a node token) never answers for a running app.
    if principal is None or not has_project_key(principal, permission_key("projects", "delete"), project_id):
        raise HTTPException(status_code=403, detail=APP_DELETE_NEEDS_PERMISSION)
    from app.hosted_apps import service

    address = service.require_address(app)
    if (confirm or "").strip().lower() != address:
        raise HTTPException(status_code=422, detail=f"Type the app's address, {address}, to delete it.")


def as_sentence(detail: dict[str, Any]) -> str:
    """The 409 in one line, for a caller that reads only a string detail (a runner's `project_update`)."""
    choices = '"retire" (archive it and remove its app)'
    if detail.get("offer_move"):
        choices = '"move" (move its code and history into the new repository) or ' + choices
    return (f"{detail.get('message', CONFLICT_MESSAGE)} This save replaces {detail.get('jaah_repo')}: send it again "
            f"with jaah_repo {choices}.")


def source_head(session: Session, replacement: Replacement) -> Optional[str]:
    """The old repo's integration-branch head (the branch its project row names now), read with the GitHub admin
    credential; None for an empty repo. 503 (nothing written) when it cannot be read — a missing branch included, so
    a repo with code is never taken for an empty one."""
    row, branch = replacement.row, replacement.old.integration_branch
    if not edition_features.enabled("easy_mode_hosting"):
        raise HTTPException(status_code=503, detail=SOURCE_UNREADABLE)
    from app.hosted_apps import config as hosting_setup, github

    try:
        token = hosting_setup.github_token(hosting_setup.github_setup())
        return github.branch_head(token, row.name, branch)
    except (hosting_setup.NotSetUp, github.GitHubError, github.GitHubUnreachable) as exc:
        log.warning("jaah repo %s: head of %s not read: %s", row.name, branch, exc)
        raise HTTPException(status_code=503, detail=SOURCE_UNREADABLE) from None


def apply(session: Session, project: Project, replacement: Replacement, choice: str, head: Optional[str],
          actor: AdminUser) -> None:
    """The row's half of the save, after the repositories were written and before the commit."""
    row = replacement.row
    if choice == MOVE:
        _keep_connected(session, project, replacement.old, actor)
        jaah_repos.move_to(row, "migrating")
        row.source_head_sha = head
        row.migration_task_id = None
    else:
        jaah_repos.move_to(row, "retiring")
    row.retire_failures = 0
    project.easy_mode = False
    stamp_actor(row, actor)


def names_primary(session: Session, row: JaahRepo, project_id: Optional[int]) -> bool:
    """Whether `row`'s repository is the project's primary one."""
    if project_id is None:
        return False
    return any(repo.is_primary and _names_row(row, repo) for repo in project_repos.repos_of(session, project_id))


def restore_if_primary_again(session: Session, project: Project, actor: Optional[AdminUser]) -> None:
    """A save that makes the Jaah repo primary again while it is moving or retiring: the row is `active` again, its
    move forgotten, so nothing archives the project's own primary or deletes its app."""
    row = jaah_repos.row_for(session, project.id)
    if row is None or row.state not in ("migrating", "move_failed", "retiring"):
        return
    if not names_primary(session, row, project.id):
        return
    jaah_repos.move_to(row, "active")
    row.migration_task_id = None
    row.source_head_sha = None
    row.retire_failures = 0
    if actor is not None:
        stamp_actor(row, actor)


def _keep_connected(session: Session, project: Project, old: OldRepo, actor: AdminUser) -> None:
    """The old repo appended as a non-primary repository, unless the save kept it."""
    stored = project_repos.repos_of(session, project.id)
    if any(_same(repo, old.repo_url, old.forge, old.forge_base_url) for repo in stored):
        return
    specs = [
        project_repos.RepoSpec(id=repo.id, repo_url=repo.repo_url, forge=repo.forge,
                               forge_base_url=repo.forge_base_url, integration_branch=repo.integration_branch,
                               is_primary=repo.is_primary)
        for repo in stored
    ]
    specs.append(project_repos.RepoSpec(id=None, repo_url=old.repo_url, forge=old.forge,
                                        forge_base_url=old.forge_base_url,
                                        integration_branch=old.integration_branch, is_primary=False))
    project_repos.replace(session, project, specs, actor.id)


def move_task_body(row: JaahRepo, old: Optional[ProjectRepo], new: str) -> str:
    where = f" (connected read-only as repository {old.id})" if old is not None else ""
    return (f"Move the code and history of the Jaah repository {row.name}{where}, integration branch "
            f"`{row.integration_branch}`, into this project's repository {new}. Code and git history only.")


def file_move_task(session: Session, project: Project, actor: AdminUser, principal: Optional[Principal]
                   ) -> Optional[int]:
    """File and start the `repo_move` task on a just-committed move, as the saver (`project_onboarding`'s create and
    Start), and record it on the row; its id, or None when nothing was filed — the row then has no task and the
    retire cron marks it `move_failed`, archiving nothing."""
    from app.routes.tasks import create, dispatch

    row = jaah_repos.row_for(session, project.id)
    if row is None or row.state != "migrating":
        return None
    ref = forges.repo_ref_or_none(project)
    if ref is None:
        log.warning("repo move task not filed for project %s: its primary repository does not parse", project.id)
        return None
    new = ref.path
    old = next((repo for repo in project_repos.repos_of(session, project.id)
                if not repo.is_primary and _names_row(row, repo)), None)
    try:
        created = create.create_task.session_body(
            body=create.ServerTaskCreate(
                project_id=project.id, persona=REPO_MOVE_PERSONA, autonomous=True,
                title=f"Move code to {new}", body=move_task_body(row, old, new),
            ),
            _user=actor, principal=principal, session=session,
        )   # commits
    except Exception as exc:  # noqa: BLE001 - never a 500 on a committed save; the cron records the failed move
        session.rollback()
        log.warning("repo move task not filed for project %s: %s", project.id, getattr(exc, "detail", exc))
        return None
    row = jaah_repos.row_for(session, project.id)
    if row is not None:
        row.migration_task_id = created.id
        session.commit()
    if not has_project_key(principal, permission_key("tasks", "edit"), project.id):
        log.info("repo move task %s filed but not started: its saver may not start tasks", created.id)
        return created.id
    try:
        dispatch.start_task.session_body(task_id=created.id, body=None, _user=actor, principal=principal,
                                         session=session)
    except Exception as exc:  # noqa: BLE001 - a draft move is the person's to start; the cron waits on it
        session.rollback()
        log.warning("repo move task %s filed but not started: %s", created.id, getattr(exc, "detail", exc))
    return created.id


def open_move_task_id(session: Session, project_id: int) -> Optional[int]:
    """The task moving the project off its Jaah repo, while that move is open (`migrating` or `move_failed`)."""
    row = jaah_repos.row_for(session, project_id)
    return row.migration_task_id if row is not None and row.state in ("migrating", "move_failed") else None
