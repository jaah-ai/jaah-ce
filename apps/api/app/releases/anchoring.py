"""Releases meet the deploy ledger (architecture §5.1, §5.2): learning a stage's deploy environment, its address, and
the deploy that made a release live. The ledger (`app/production_qa/deployments.py`) owns every deploy fact; a release
only links one (`releases.deployment_id`, `deployments.release_id`).

LEARNING — while a stage after Dev has no `deploy_environment`, every push-triggered run on its branch is recorded
whatever `projects.deploy_workflows` says (`learning`). The first such run that succeeds, ships an environment and
whose head carries a release's merge into the stage teaches it: the stage takes that environment and the project
watches that workflow from then on (a side workflow on that branch teaches nothing). Dev records no extra run — the
integration branch runs every CI workflow — and learns from a watched one: once Production taught the project its
deploy workflow, that workflow's run on the integration branch teaches Dev.

ADDRESSES — a successful `deployment_status` carrying an `environment_url` writes it to the stage whose environment
it is (or, still learning, whose branch the run is on), unless a person typed the address (`url_source = manual`) or
Easy Mode owns it.

ANCHORING — a concluded push run that shipped a stage's environment and whose head carries a release's merge sha:
success makes a `deploying` release `live` (a `shipped` one too, or one failed `deploy_failed`/`no_deploy` — a re-run
that landed — within `SHIPPED_ANCHOR_WINDOW`); failure fails a `deploying` one `{deploy_failed, open_run}`. Dev
anchors a back-merge. The stage's `deploying` arrival the run carries settles with it (`arrivals.on_deploy`), the
integration stage's included. Called from the ledger's reconcile and, for a run reconciled before its
release reached `deploying`, from the release tick (`sweep`).

STATUS ANCHORING (`on_status`) — a GitHub `deployment_status` that no recorded push run carries (GitHub Pages' own
"pages build and deployment" run, a `dynamic` or `workflow_dispatch` run, an unwatched workflow) settles the same
releases: `success` for a stage's environment whose sha is, or carries (`carries`: GitHub's compare), a release's
merge makes it `live`; `failure`/`error` fails a `deploying` one `{deploy_failed, open_run}`, the run named by the
status's log URL. A stage still learning takes the environment from a successful one whose `deployment.ref` is its
branch and that carries one of its moving releases. A status of a recorded push run is left to that run. Nothing here
commits.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.models.production_qa import Deployment, DeploymentCommit
from app.models.projects import Project
from app.models.releases import Release, ReleaseStage
from app.releases import arrivals, lifecycle, service, snapshot, stages as stages_mod

log = logging.getLogger(__name__)

#: A `shipped` release (merged where no deploy was tracked yet) still turns `live` when a deploy of it lands this soon.
SHIPPED_ANCHOR_WINDOW = timedelta(hours=2)
#: The `url_source` values a detected address never overwrites.
_KEPT_URL_SOURCES = ("manual", "easy_mode")
_FAILED = ("failure", "timed_out", "startup_failure")
#: The `deployment_status` states that fail a `deploying` release.
_STATUS_FAILED = ("failure", "error")
#: GitHub's compare answers meaning the deployed sha carries the merge.
_CARRIED = ("ahead", "identical")
_SHA = re.compile(r"[0-9a-f]{40}")
_COMPARE_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=2.0)


def _stages(session: Session, project: Project) -> list[ReleaseStage]:
    """The branch-level stages: the ones a deploy lands on (`stages.branch_stages`)."""
    return stages_mod.branch_stages(stages_mod.stages_of(session, project.id))


def _learning_stage(session: Session, project: Project, branch: Optional[str]) -> Optional[ReleaseStage]:
    """The stage still learning its environment whose branch is `branch`."""
    if not branch:
        return None
    return next((s for s in _stages(session, project)
                 if s.deploy_environment is None and stages_mod.branch_of(project, s) == branch), None)


def learning(session: Session, project: Project, branch: Optional[str], run_event: Optional[str]) -> bool:
    """Whether a run of `run_event` on `branch` is recorded although no deploy workflow names it: a push run on a
    stage after Dev that has not learned its environment yet."""
    if run_event != "push":
        return False
    stage = _learning_stage(session, project, branch)
    return stage is not None and not stages_mod.is_integration(stage)


def _carries(session: Session, deployment: Deployment, sha: Optional[str]) -> bool:
    """Whether `deployment`'s head carries commit `sha`: it is the head, or in the range the ledger recorded."""
    if not sha:
        return False
    if deployment.head_sha == sha:
        return True
    return bool(session.scalar(select(exists().where(
        DeploymentCommit.deployment_id == deployment.id, DeploymentCommit.sha == sha))))


def _shipped_success(deployment: Deployment) -> bool:
    return deployment.conclusion_raw == "success" and deployment.shipped and deployment.environment not in (None, "unknown")


#: The failures a later successful deploy of the same release undoes.
_UNDONE_BY_A_DEPLOY = ("deploy_failed", "no_deploy")


def _moving(session: Session, project: Project, stage: ReleaseStage, now: datetime,
            only: Optional[list[int]] = None) -> list[Release]:
    """The releases into `stage` a deploy may anchor: `deploying`; `shipped`, or failed for want of a deploy, within
    the window. `only`: those of these ids, locked and read afresh."""
    recent = Release.updated_at >= now - SHIPPED_ANCHOR_WINDOW
    stmt = select(Release).where(
        Release.project_id == project.id, Release.to_stage_id == stage.id, Release.merge_sha.is_not(None),
        or_(Release.state == lifecycle.DEPLOYING,
            (Release.state == lifecycle.SHIPPED) & recent,
            (Release.state == lifecycle.FAILED) & recent
            & Release.failure["kind"].astext.in_(_UNDONE_BY_A_DEPLOY)),
    ).order_by(Release.id)
    if only is not None:
        stmt = stmt.where(Release.id.in_(only)).with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(stmt))


def learn(session: Session, project: Project, deployment: Deployment, now: datetime) -> bool:
    """Teach the learning stage on this run's branch its environment, when this run may. True when it did (the
    caller re-classifies: the production environment may have just become known)."""
    if deployment.run_event != "push" or not _shipped_success(deployment):
        return False
    stage = _learning_stage(session, project, deployment.head_branch)
    if stage is None:
        return False
    taken = {s.deploy_environment for s in _stages(session, project) if s.deploy_environment}
    if deployment.environment in taken:
        return False
    if not stages_mod.is_integration(stage) and not any(
            _carries(session, deployment, r.merge_sha) for r in _moving(session, project, stage, now)):
        return False
    stage.deploy_environment = deployment.environment
    path = deployment.workflow_path
    if path and path not in (project.deploy_workflows or []):
        project.deploy_workflows = [*(project.deploy_workflows or []), path]
    session.flush()
    log.info("releases: project %s stage %s learned environment %s from deploy %s", project.id, stage.name,
             deployment.environment, deployment.id)
    return True


def record_url(session: Session, project: Project, deployment: Deployment, environment: str, state: str,
               url: Optional[str]) -> None:
    """A `deployment_status` reported `url` for `environment`: the stage's address, unless a person or Easy Mode set it."""
    url = (url or "").strip()
    if state != "success" or not url.startswith(("https://", "http://")):
        return
    stages = _stages(session, project)
    stage = next((s for s in stages if s.deploy_environment == environment), None)
    if stage is None:
        learner = _learning_stage(session, project, deployment.head_branch)
        # Still learning: the run's branch names the stage; never overwrite an address with a side job's.
        stage = learner if learner is not None and not learner.url else None
    if stage is not None:
        _set_url(session, stage, url)


def _set_url(session: Session, stage: ReleaseStage, url: str) -> None:
    if stage.url_source in _KEPT_URL_SOURCES or stage.url == url:
        return
    stage.url, stage.url_source = url, "detected"
    session.flush()


def anchor(session: Session, project: Project, deployment: Deployment, now: datetime) -> list[Release]:
    """Link this concluded push run to the releases it carries into its stage; the releases it moved."""
    if deployment.run_event != "push" or deployment.conclusion_raw is None or not deployment.environment:
        return []
    stage = next((s for s in _stages(session, project) if s.deploy_environment == deployment.environment), None)
    if stage is None:
        return []
    success = deployment.status == "success" and deployment.shipped
    failure = deployment.status == "failure" or deployment.conclusion_raw in _FAILED
    if not (success or failure):
        return []
    moved: list[Release] = []
    for release in _moving(session, project, stage, now):
        if not _carries(session, deployment, release.merge_sha):
            continue
        if not _settle(release, success,
                       {"kind": "deploy_failed", "message": deployment.failing_job, "action": "open_run"}):
            continue
        release.deployment_id = deployment.id
        deployment.release_id = release.id
        moved.append(release)
    if (success or failure) and arrivals.on_deploy(
            session, project, stage, lambda sha: _carries(session, deployment, sha), success,
            report=deployment.failing_job):
        service.wake_tick(session)
    _moved(session, moved)
    return moved


def _settle(release: Release, ok: bool, failure: dict) -> bool:
    """A deploy carrying `release` into its stage concluded: success makes it `live`; a failure fails it with `failure`
    when it is `deploying`. True when it moved."""
    if ok:
        service._move(release, "anchor", lifecycle.LIVE)
        release.failure = None
        return True
    if release.state != lifecycle.DEPLOYING:
        return False
    service._move(release, "fail", lifecycle.FAILED)
    release.failure = failure
    return True


def _moved(session: Session, moved: list[Release]) -> None:
    if moved:
        session.flush()
        service.wake_tick(session)


class _Carries:
    """Whether deployed commit `sha` is, or descends from, a release's merge: equal, else GitHub's compare of the two
    reads `ahead`/`identical`, read with one `github_hooks.repo_token` (purpose `contents`) resolved on first need.
    Any failure reads False: the next status, or the release tick's `no_deploy`, still decides."""

    def __init__(self, session: Session, project: Project, sha: str) -> None:
        self.session, self.project, self.sha = session, project, sha
        self._token: object = None
        self._resolved = False
        self._known: dict[str, bool] = {}

    def __call__(self, merge_sha: Optional[str]) -> bool:
        if not merge_sha:
            return False
        if merge_sha == self.sha:
            return True
        if merge_sha not in self._known:
            self._known[merge_sha] = self._compare(merge_sha)
        return self._known[merge_sha]

    def _compare(self, merge_sha: str) -> bool:
        if not (_SHA.fullmatch(self.sha) and _SHA.fullmatch(merge_sha)):
            return False
        from app import github_api
        from app.production_qa import github_hooks

        try:
            if not self._resolved:
                self._resolved = True
                self._token = github_hooks.repo_token(self.session, self.project, purpose="contents")
            token = self._token
            if not isinstance(token, github_hooks.RepoToken):
                return False
            owner, _, repo = token.owner_repo.partition("/")
            path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/compare/{merge_sha}...{self.sha}"
            status, body = github_api.get_json(token.token, path, timeout=_COMPARE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — any failure is "not carried" (class docstring), never the delivery's
            log.info("releases: compare %s...%s on project %s failed: %s", merge_sha, self.sha, self.project.id,
                     github_api.reason(exc))
            return False
        return status == 200 and isinstance(body, dict) and body.get("status") in _CARRIED


def on_status(session: Session, project: Project, *, environment: str, sha: str, ref: Optional[str], state: str,
              url: Optional[str], link: Optional[str], deployment: Optional[Deployment],
              now: datetime) -> list[Release]:
    """A GitHub `deployment_status` no recorded push run carries (module docstring, STATUS ANCHORING): learn, take
    the address, and settle the releases its sha carries into the environment's stage; the releases it moved.
    `link`: the status's run page, named by a failure; `deployment`: the ledger's run of it, when recorded."""
    ok = state == "success"
    if not (ok or state in _STATUS_FAILED) or not environment or not sha:
        return []
    carried = _Carries(session, project, sha)
    stages = _stages(session, project)
    stage = next((s for s in stages if s.deploy_environment == environment), None)
    if stage is None:
        stage = _learn_from_status(session, project, stages, environment, ref, ok, now, carried)
    if stage is None:
        return []
    url = (url or "").strip()
    if ok and url.startswith(("https://", "http://")):
        _set_url(session, stage, url)
    # The stage's own arrival (the integration stage's too, which no release carries): `arrivals.on_deploy`.
    if arrivals.on_deploy(session, project, stage, carried, ok, url=url,
                          report=deployment.failing_job if deployment is not None else None):
        service.wake_tick(session)
    # Compare first, unlocked (it reads GitHub); then lock what is still moving, so a tick that moved one meanwhile wins.
    ids = [r.id for r in _moving(session, project, stage, now) if carried(r.merge_sha)]
    if not ids:
        return []
    failure = {"kind": "deploy_failed", "message": deployment.failing_job if deployment is not None else None,
               "action": "open_run", "url": link}
    moved: list[Release] = []
    for release in _moving(session, project, stage, now, only=ids):
        if not _settle(release, ok, failure):
            continue
        if deployment is not None:
            release.deployment_id = deployment.id
            deployment.release_id = release.id
        moved.append(release)
        log.info("releases: release %s %s by deployment_status %s of %s", release.id, release.state, state,
                 environment)
    _moved(session, moved)
    return moved


def _learn_from_status(session: Session, project: Project, stages: list[ReleaseStage], environment: str,
                       ref: Optional[str], ok: bool, now: datetime,
                       carried: Callable[[Optional[str]], bool]) -> Optional[ReleaseStage]:
    """The learning stage after Dev whose branch is the deployment's `ref`, taught `environment` by a successful
    status carrying one of its moving releases; None when nothing was learned."""
    branch = (ref or "").removeprefix("refs/heads/")
    stage = _learning_stage(session, project, branch) if ok else None
    if stage is None or stages_mod.is_integration(stage) or environment in {s.deploy_environment for s in stages}:
        return None
    if not any(carried(r.merge_sha) for r in _moving(session, project, stage, now)):
        return None
    stage.deploy_environment = environment
    session.flush()
    log.info("releases: project %s stage %s learned environment %s from a deployment_status", project.id,
             stage.name, environment)
    return stage


def sweep(session: Session, project: Project, release: Release, now: datetime) -> bool:
    """The tick's look-back for one moving release: a run of its stage's branch already reconciled before the
    release reached `deploying` (a fast CI), learned from and anchored now. True when the release moved."""
    stage = session.scalar(select(ReleaseStage).where(ReleaseStage.id == release.to_stage_id))
    if stage is None or not release.merge_sha:
        return False
    # The branch it was requested into (`snapshot`): a save since never moves a running release.
    branch = snapshot.to_branch(project, release, stage) or stages_mod.branch_of(project, stage)
    runs = list(session.scalars(select(Deployment).where(
        Deployment.project_id == project.id, Deployment.head_branch == branch, Deployment.run_event == "push",
        Deployment.conclusion_raw.is_not(None), Deployment.release_id.is_(None),
        or_(Deployment.head_sha == release.merge_sha, exists().where(
            DeploymentCommit.deployment_id == Deployment.id, DeploymentCommit.sha == release.merge_sha)),
        Deployment.first_received_at >= release.requested_at,
    ).order_by(Deployment.id)))
    before = release.state
    for deployment in runs:
        if learn(session, project, deployment, now):
            from app.production_qa import deployments as ledger

            ledger.reconcile(session, deployment)  # re-classified now the environment is known; anchors in turn
        anchor(session, project, deployment, now)
        if release.state != before:
            return True
    return False

