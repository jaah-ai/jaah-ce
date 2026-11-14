"""Connect deploys (architecture §5.1 step 1, releases §5.3): the record-only triggers deploy ingestion listens on, made
once per project. The single writer behind Production QA's routes (`routes/production_qa.py`: GitHub/GitLab/Azure
DevOps by workflow name, Bitbucket by production branch), which the Production QA tab's Connect calls.

The forge is the one whose deploy triggers the project already has, else its first connected GitHub, GitLab, Azure
DevOps or Bitbucket repository's (primary first) — a parse of our own rows, never a forge read. On GitHub a connect
that keeps each secret sealed also installs the webhooks itself (`github_hooks.install`); every other forge answers
each new secret once, for the person to paste. A refusal raises `HTTPException` (422) with the text the routes answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.automations import TaskTrigger
from app.models.identity import AdminUser
from app.models.projects import Project
from app.production_qa import github_hooks
from app.production_qa.azure_devops import EVENTS as AZURE_DEVOPS_EVENTS
from app.releases import stages as release_stages
from app.trigger_config import GITLAB_DEPLOY_EVENTS
from app.triggers import SOURCE_AZURE_DEVOPS, SOURCE_BITBUCKET, SOURCE_GITHUB, SOURCE_GITLAB

# The record-only triggers deploy ingestion listens on, per forge (`app/production_qa/deployments.py`, GitLab's
# `gitlab_deploys.py`, Azure DevOps' `azure_devops.py`). Jobs ride their own trigger so a CI-wide job burst only meets
# its own ingress limiter; Azure DevOps has one trigger, every service hook subscription posting to it.
DEPLOY_TRIGGERS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    SOURCE_GITHUB: (
        ("Deploys", ("push", "workflow_run", "deployment_status")),
        ("Deploy jobs", ("workflow_job",)),
    ),
    SOURCE_GITLAB: (
        ("Deploys", tuple(e for e in GITLAB_DEPLOY_EVENTS if e != "Job Hook")),
        ("Deploy jobs", ("Job Hook",)),
    ),
    SOURCE_AZURE_DEVOPS: (("Azure DevOps deploys", AZURE_DEVOPS_EVENTS),),
}
# Bitbucket's one record-only trigger names a production branch, not workflow files (`app/production_qa/bitbucket.py`).
DEPLOY_SOURCE_KINDS = (*DEPLOY_TRIGGERS, SOURCE_BITBUCKET)
RECORDS_ONLY_TEMPLATE = "Records deploys; files nothing."
NO_REPOSITORY = "Connect a GitHub, GitLab, Azure DevOps or Bitbucket repository to this project first."
NO_BITBUCKET_REPOSITORY = "Connect a Bitbucket repository to this project first."
ON_BITBUCKET = "This project is on Bitbucket: name its production branch instead."
NO_WORKFLOW = "Name at least one deploy workflow."


@dataclass
class Connected:
    """What a connect left: the forge, the project's deploy workflows, each deploy trigger with the secret this
    connect created it with (None for one that already existed, or when the reveal is kept owed), and, on GitHub when
    the reveal is kept owed, whether the webhooks were installed."""

    forge: str
    deploy_workflows: list[str]
    triggers: list[tuple[TaskTrigger, Optional[str]]] = field(default_factory=list)
    installed: Optional[github_hooks.InstallResult] = None


def events_of(trigger: TaskTrigger) -> list[str]:
    """The events a deploy trigger listens to: its config's, or every Azure DevOps service hook it records."""
    if trigger.source_kind == SOURCE_AZURE_DEVOPS:
        return list(AZURE_DEVOPS_EVENTS)
    return list((trigger.source_config or {}).get("events") or [])


def deploy_triggers(session: Session, project_id: int) -> dict[tuple[str, str], TaskTrigger]:
    """The project's record-only deploy triggers, by (kind, the event set they listen to)."""
    from app.production_qa import recorder

    out: dict[tuple[str, str], TaskTrigger] = {}
    for trigger in session.scalars(
        select(TaskTrigger).where(TaskTrigger.project_id == project_id,
                                  TaskTrigger.source_kind.in_(DEPLOY_SOURCE_KINDS))
        .order_by(TaskTrigger.created_at)
    ):
        if recorder.records_only(trigger):
            out.setdefault((trigger.source_kind, ",".join(sorted(events_of(trigger)))), trigger)
    return out


def deploy_repositories(session: Session, project: Project) -> list[tuple[str, str]]:
    """`(forge, repository path)` of every connected GitHub, GitLab, Azure DevOps or Bitbucket Cloud repository,
    primary first — a parse of our own rows (a self-managed GitLab with its base URL), never a forge read."""
    from app import forges, project_repos

    out: list[tuple[str, str]] = []
    for row in sorted(project_repos.repos_of(session, project.id), key=lambda r: not r.is_primary):
        try:
            ref = forges.parse_repo_url(row.repo_url or "", row.forge if row.forge == "gitlab" else None,
                                        row.forge_base_url)
        except forges.ForgeUrlError:
            continue
        if ref.forge in DEPLOY_SOURCE_KINDS and (ref.forge, ref.path) not in out:
            out.append((ref.forge, ref.path))
    return out


def deploy_forge(session: Session, project: Project) -> tuple[Optional[str], Optional[str]]:
    """(forge, repository) a connect uses (module docstring); (None, None) when the project has none."""
    found = deploy_repositories(session, project)
    held = {kind for kind, _events in deploy_triggers(session, project.id)}
    for forge, repository in found:
        if forge in held:
            return forge, repository
    if held and not found:
        return sorted(held)[0], None
    return found[0] if found else (None, None)


def connect(session: Session, project: Project, actor: AdminUser, *, workflows: Optional[list[str]] = None,
            production_branch: Optional[str] = None, statuses: Optional[list[str]] = None,
            forge: Optional[str] = None, reveal: bool = True) -> Connected:
    """Create whichever record-only deploy triggers the project lacks.

    `workflows` (GitHub, GitLab, Azure DevOps): the deploy workflow paths to add, at least one — none on a GitHub
    repository whose Pages build from the branch (`github_hooks.detect_workflows`: no workflow file to name); None
    names none. `production_branch`/`statuses`: Bitbucket's, required there; a second Bitbucket connect updates them.
    `forge`: connect that forge's first repository rather than the project's (Bitbucket's own route). `reveal=False`
    keeps each new secret's one reveal owed and, on GitHub, installs the webhooks from the sealed secrets
    (`Connected.installed`). Commits each created trigger (`write_trigger_create`); the caller commits the rest."""
    if forge == SOURCE_BITBUCKET:
        repositories = [path for kind, path in deploy_repositories(session, project) if kind == SOURCE_BITBUCKET]
        if not repositories:
            raise HTTPException(status_code=422, detail=NO_BITBUCKET_REPOSITORY)
        return _connect_bitbucket(session, project, actor, repositories[0], production_branch, statuses or [], reveal)
    found, repository = deploy_forge(session, project)
    if found is None or repository is None:
        # A webhook naming no repository would record every repository the forge sends it.
        raise HTTPException(status_code=422, detail=NO_REPOSITORY)
    if found == SOURCE_BITBUCKET:
        if production_branch is None:
            raise HTTPException(status_code=422, detail=ON_BITBUCKET)
        return _connect_bitbucket(session, project, actor, repository, production_branch, statuses or [], reveal)
    merged: Optional[list[str]] = None
    if workflows is not None and not (found == SOURCE_GITHUB and not any(w.strip() for w in workflows)
                                      and _pages_branch_built(session, project, actor)):
        merged = _merged_workflows(project, workflows, found)
    existing = deploy_triggers(session, project.id)
    hooks_token: Optional[github_hooks.RepoToken] = None
    if found == SOURCE_GITHUB and not reveal:
        # Resolved before any trigger is made: a connect nothing can install leaves nothing behind — the workflows
        # included, since saved workflows alone read as connected (`releases/views.py::deploy_events`) and a release
        # would then wait for a deploy nothing records. The answer still echoes them, for the next try.
        hooks_token, refused = github_hooks.hooks_token(session, project, actor_id=actor.id)
        if hooks_token is None:
            return Connected(forge=found, deploy_workflows=list(merged if merged is not None
                                                                else project.deploy_workflows or []),
                             triggers=[(t, None) for (kind, _e), t in existing.items() if kind == found],
                             installed=refused)
    if merged is not None:
        project.deploy_workflows = merged
    out = Connected(forge=found, deploy_workflows=[])
    for name, events in DEPLOY_TRIGGERS[found]:
        key = (found, ",".join(sorted(events)))
        if key in existing:
            out.triggers.append((existing[key], None))
            continue
        config: dict[str, Any] = {"record_only": True, "repository": repository}
        if found == SOURCE_GITHUB:
            # A new GitHub trigger listens to the primary repository by default (`routes/triggers.py`).
            config = {"events": list(events), "record_only": True, "actions": []}
        elif found == SOURCE_GITLAB:
            config["events"] = list(events)
        try:
            trigger, secret = _create(session, project, actor, name, found, config, reveal)
        except HTTPException:
            # Each create commits its own trigger, its secret revealed in this answer: one already created is
            # answered rather than lost behind the second's refusal (Connect again creates the missing one).
            if any(secret for _t, secret in out.triggers):
                break
            raise
        out.triggers.append((trigger, secret))
    out.deploy_workflows = list(project.deploy_workflows or [])
    if found == SOURCE_GITHUB and not reveal:
        out.installed = github_hooks.install(session, project, [t for t, _secret in out.triggers], actor_id=actor.id,
                                             token=hooks_token)
    return out


def reveal_owed(session: Session, connected: Connected) -> Connected:
    """`connected` with each trigger whose one reveal is still owed answering its secret now, the reveal spent: the
    manual setup a refused install falls back to. Flushes; the caller commits."""
    from app import trigger_secrets
    from app.clock import utcnow

    out: list[tuple[TaskTrigger, Optional[str]]] = []
    for trigger, secret in connected.triggers:
        if secret is None and trigger.secret_sealed is not None and trigger.secret_revealed_at is None:
            try:
                secret = trigger_secrets.open_secret(trigger)
            except ValueError:  # sealed under a retired key: rotate it on the trigger's page
                secret = None
            else:
                trigger.secret_revealed_at = utcnow()
        out.append((trigger, secret))
    connected.triggers = out
    session.flush()
    return connected


def production_branch_of(session: Session, project: Project) -> str:
    """The Production stage's branch (`releases/stages.py`), else the project's integration branch."""
    production = release_stages.production(project, session)
    return release_stages.branch_of(project, production) if production is not None else project.integration_branch


def _pages_branch_built(session: Session, project: Project, actor: AdminUser) -> bool:
    return github_hooks.detect_workflows(session, project, production_branch_of(session, project),
                                         actor_id=actor.id).pages_branch_built


def _merged_workflows(project: Project, workflows: list[str], forge: str) -> list[str]:
    from app.routes.projects import _validated_production_qa

    asked = _validated_production_qa({"deploy_workflows": workflows}, forge)["deploy_workflows"]
    if not asked:
        raise HTTPException(status_code=422, detail=NO_WORKFLOW)
    merged = _validated_production_qa({"deploy_workflows": [*(project.deploy_workflows or []), *asked]}, forge)
    return list(merged["deploy_workflows"])


def _create(session: Session, project: Project, actor: AdminUser, name: str, forge: str, config: dict[str, Any],
            reveal: bool) -> tuple[TaskTrigger, Optional[str]]:
    from app.routes.triggers import TriggerCreate, write_trigger_create

    return write_trigger_create(session, TriggerCreate(
        project_id=project.id, name=name, description=RECORDS_ONLY_TEMPLATE, source_kind=forge,
        source_config=config, persona="coder", prompt_template=RECORDS_ONLY_TEMPLATE,
    ), actor, reveal_now=reveal)


def _connect_bitbucket(session: Session, project: Project, actor: AdminUser, repository: str,
                       production_branch: Optional[str], statuses: list[str], reveal: bool) -> Connected:
    """The project's one record-only Bitbucket deploy trigger; a second connect updates its production branch and
    statuses. Names the Production stage's environment `production` when it names none (the environment a
    production-branch status ships to)."""
    from app.actors import stamp_actor
    from app.entity_shape import refused_as
    from app.production_qa.bitbucket import DEFAULT_ENVIRONMENT
    from app.trigger_config import BITBUCKET_DEPLOY_EVENTS, SourceConfigError, validate_source_config

    with refused_as(422, SourceConfigError):
        config = validate_source_config(SOURCE_BITBUCKET, {
            "events": list(BITBUCKET_DEPLOY_EVENTS), "repository": repository,
            "production_branch": production_branch, "statuses": statuses,
        })
    production = release_stages.production(project, session)
    if production is not None and not production.deploy_environment:
        production.deploy_environment = DEFAULT_ENVIRONMENT
    existing = next((t for (kind, _events), t in deploy_triggers(session, project.id).items()
                     if kind == SOURCE_BITBUCKET), None)
    out = Connected(forge=SOURCE_BITBUCKET, deploy_workflows=list(project.deploy_workflows or []))
    if existing is not None:
        existing.source_config = {**config, "repository": (existing.source_config or {}).get("repository")
                                  or config["repository"]}
        stamp_actor(existing, actor)
        out.triggers.append((existing, None))
        return out
    session.flush()
    out.triggers.append(_create(session, project, actor, "Bitbucket deploys", SOURCE_BITBUCKET, config, reveal))
    return out
