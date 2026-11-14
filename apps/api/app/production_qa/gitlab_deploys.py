"""GitLab deploy ingestion: a `gitlab` trigger's Push, Pipeline, Job and Deployment Hooks, recorded onto the same
`branch_pushes`, `deployments` and `deployment_environments` rows, ranges, PRs, `deployed` edges and `deploy_failed`
incidents as GitHub's (`deployments.py`). Each hook is trimmed, then translated into the GitHub-shaped event
`deployments.py` records, and the deployment it touched is reconciled there; classification, range and effects are
that module's, unchanged.

Vendor docs: https://docs.gitlab.com/user/project/integrations/webhook_events/ and
https://docs.gitlab.com/user/project/integrations/webhooks/ (read 2026-11-01).

WHERE THE FACTS COME FROM — two record-only `gitlab` triggers (Connect deploys, `routes/production_qa.py`): *Deploys*
(`Push Hook`, `Pipeline Hook`, `Deployment Hook`) and *Deploy jobs* (`Job Hook`, a CI-wide stream on its own ingress
limiter). The sender proves itself with GitLab's Secret token, `X-Gitlab-Token`, compared in constant time
(`trigger_secrets.verify`); `Idempotency-Key`, constant across GitLab's retries, is the dedup key. Only an event the
trigger's config lists, for the project path it names (`project.path_with_namespace`, case-insensitive), is read.

THE MAP
* `Push Hook` → a `branch_pushes` row. GitLab lists the newest 20 commits, so a push whose `total_commits_count` is
  larger is `truncated`; GitLab does not say whether a push was forced (`forced` false — a forced push breaks the
  `before` chain and reads `partial` there).
* `Pipeline Hook` → one `deployments` row per pipeline (`run_id` = the pipeline id, `run_attempt` 1) when its CI
  config (`project.ci_config_path`, default `.gitlab-ci.yml`) is one of `projects.deploy_workflows` AND one of its
  jobs deploys (an `environment` whose action is `start` or unset) — a test-only pipeline is no deploy. `source` is
  the run event (`push` anchors, as GitHub's), `ref` the branch (none for a tag), `url` the run link. Status:
  `success` → success, `failed` → failure, `canceled` → cancelled, `skipped` → skipped; `running` in progress;
  any other → queued. A retried pipeline reports a later terminal status under the same id: a terminal status whose
  `finished_at` is later than the recorded one replaces the earlier conclusion and failing job.
  A failed pipeline names its failing job from its `builds` (the first failed one not `allow_failure`) when no Job Hook
  did.
* `Job Hook` → a failed job (not `allow_failure`) names its pipeline's failing job, first reported wins. GitLab jobs
  have no steps (`failing_step` stays empty).
* `Deployment Hook` → one `deployment_environments` row: `success` → success, `failed` → failure, `canceled` → error,
  `running` → in_progress, `blocked`/`created` → pending (an approval event is not a state). The payload carries no
  pipeline id, so it is correlated by the commit (the SHA in `commit_url`) and the CI config path, the latest pipeline
  first; one that arrives before its pipeline is re-correlated by the `gitlab_deploy_recorrelate` job for
  `deployments.RECORRELATE_WINDOW`, from the trimmed copy kept in its delivery's preview.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import parse_utc
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.production_qa import Deployment
from app.models.projects import Project
from app.production_qa import cron, deployments, recorder
from app.production_qa.events import RaiseIntent
from app.production_qa.vocabulary import REASON_RECORDED
from app.triggers import FILTERED, GITLAB_EVENT_HEADER, SOURCE_GITLAB


PUSH = "Push Hook"
PIPELINE = "Pipeline Hook"
JOB = "Job Hook"
DEPLOYMENT = "Deployment Hook"
_EVENTS = (PUSH, PIPELINE, JOB, DEPLOYMENT)

DEFAULT_CI_CONFIG = ".gitlab-ci.yml"
_CONCLUSIONS = {"success": "success", "failed": "failure", "canceled": "cancelled", "skipped": "skipped"}
_ENVIRONMENT_STATES = {"success": "success", "failed": "failure", "canceled": "error", "running": "in_progress",
                       "blocked": "pending", "created": "pending"}
_COMMIT_IN_URL = re.compile(r"/commit/([0-9a-fA-F]{40})(?:[/?#]|$)")


def _ts(value: Any) -> Optional[datetime]:
    """GitLab's timestamps: ISO 8601, or the older `2016-08-12 15:23:28 UTC`."""
    text = str(value or "").strip()
    if text.endswith(" UTC"):
        text = text[:-4].replace(" ", "T") + "+00:00"
    return parse_utc(text)


def _ci_config(project: dict[str, Any]) -> str:
    return str(project.get("ci_config_path") or "").strip() or DEFAULT_CI_CONFIG


# ── The stored event ────────────────────────────────────────────────────────────────────────────────

def _trimmed(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Only the fields this module reads of each hook."""
    project = body.get("project") or {}
    out: dict[str, Any] = {"project": {k: project.get(k) for k in ("path_with_namespace", "ci_config_path")}}
    if name == PUSH:
        out.update({k: body.get(k) for k in ("ref", "before", "after", "total_commits_count")})
        out["commits"] = [{"id": c.get("id"), "timestamp": c.get("timestamp")}
                          for c in body.get("commits") or [] if isinstance(c, dict)]
    elif name == PIPELINE:
        attrs = body.get("object_attributes") or {}
        out["object_attributes"] = {k: attrs.get(k) for k in (
            "id", "name", "ref", "tag", "sha", "source", "status", "created_at", "finished_at", "url")}
        out["builds"] = [{
            **{k: b.get(k) for k in ("id", "name", "status", "allow_failure", "finished_at")},
            "environment": ({k: b["environment"].get(k) for k in ("name", "action")}
                            if isinstance(b.get("environment"), dict) else None),
        } for b in body.get("builds") or [] if isinstance(b, dict)]
    elif name == JOB:
        out.update({k: body.get(k) for k in (
            "build_id", "build_name", "build_status", "build_allow_failure", "pipeline_id")})
    else:
        out.update({k: body.get(k) for k in (
            "status", "status_changed_at", "deployment_id", "deployable_url", "environment", "commit_url")})
    return out


def _stored_event(trigger: TaskTrigger, stored: Any) -> Optional[deployments._Event]:
    """The hook a delivery's stored `{headers, body}` carries (`deployments.stored_event`): `repo_path` is the project
    path, `project.path_with_namespace`."""
    return deployments.stored_event(
        trigger, stored, source_kind=SOURCE_GITLAB, header=GITLAB_EVENT_HEADER, names=_EVENTS, require_named=True,
        repo_of=lambda body: (body.get("project") or {}).get("path_with_namespace"))


# ── The translation onto `deployments.py` ───────────────────────────────────────────────────────────

def _record_push(session: Session, project: Project, body: dict[str, Any], repo_path: str,
                 dedup_key: Optional[str], received: datetime) -> list[Deployment]:
    commits = [c for c in body.get("commits") or [] if isinstance(c, dict)]
    after = body.get("after")
    head = next((c for c in commits if c.get("id") == after), commits[-1] if commits else {})
    stamp = _ts(head.get("timestamp"))
    shaped = {"ref": body.get("ref"), "before": body.get("before"), "after": after, "forced": False,
              "commits": [{"id": c.get("id")} for c in commits],
              "head_commit": {"id": after, "timestamp": stamp.isoformat() if stamp else None}}
    total = body.get("total_commits_count")
    truncated = isinstance(total, int) and total > len(commits)
    return deployments._record_push(session, project, deployments._Event("push", shaped, repo_path), dedup_key,
                                    received, truncated=truncated)


def _deploys(builds: list[dict[str, Any]]) -> bool:
    """Whether one of the pipeline's jobs deploys: an environment it starts (an unset action is `start`)."""
    return any(isinstance(b.get("environment"), dict) and b["environment"].get("name")
               and (b["environment"].get("action") or "start") == "start" for b in builds)


def _failing_build(builds: list[dict[str, Any]]) -> Optional[str]:
    failed = [b for b in builds if b.get("status") == "failed" and not b.get("allow_failure")]
    failed.sort(key=lambda b: (_ts(b.get("finished_at")) is None, _ts(b.get("finished_at")) or datetime.min,
                               b.get("id") or 0))
    return str(failed[0].get("name") or "").strip() or None if failed else None


def _job_event(pipeline_id: int, name: Optional[str], repo_path: str) -> deployments._Event:
    return deployments._Event("workflow_job", {"action": "completed", "workflow_job": {
        "run_id": pipeline_id, "run_attempt": 1, "name": name, "conclusion": "failure", "steps": []}}, repo_path)


def _reopen_retried(session: Session, project: Project, repo_path: str, pipeline_id: int,
                    finished: Optional[datetime]) -> None:
    """A retried pipeline's later terminal status replaces the conclusion and failing job it had (module docstring)."""
    if finished is None:
        return
    deployment = deployments._locked_run(session, project.id, repo_path, pipeline_id, 1)
    if deployment is None or deployment.conclusion_raw is None:
        return
    if deployment.finished_at is not None and finished <= deployment.finished_at:
        return
    deployment.conclusion_raw = None
    deployment.failing_job = None
    deployment.failing_step = None
    session.flush()


def _record_pipeline(session: Session, project: Project, body: dict[str, Any], repo_path: str,
                     received: datetime) -> list[Deployment]:
    attrs = body.get("object_attributes") or {}
    builds = [b for b in body.get("builds") or [] if isinstance(b, dict)]
    pipeline_id = attrs.get("id")
    if not isinstance(pipeline_id, int) or not _deploys(builds):
        return []
    status = str(attrs.get("status") or "")
    conclusion = _CONCLUSIONS.get(status)
    finished = _ts(attrs.get("finished_at"))
    if conclusion is not None:
        _reopen_retried(session, project, repo_path, pipeline_id, finished)
    run = {
        "id": pipeline_id, "run_attempt": 1, "name": attrs.get("name"), "path": _ci_config(body.get("project") or {}),
        "event": attrs.get("source"),
        "status": "completed" if conclusion else ("in_progress" if status == "running" else "queued"),
        "conclusion": conclusion, "head_branch": None if attrs.get("tag") else attrs.get("ref"),
        "head_sha": attrs.get("sha"), "html_url": attrs.get("url"),
        "run_started_at": (_ts(attrs.get("created_at")) or received).isoformat(),
        "updated_at": finished.isoformat() if finished else None,
    }
    touched = deployments._record_run(
        session, project, deployments._Event("workflow_run", {"workflow_run": run}, repo_path), received)
    if conclusion == "failure" and touched and touched[0].failing_job is None:
        deployments._record_job(session, project, _job_event(pipeline_id, _failing_build(builds), repo_path))
    return touched


def _record_job(session: Session, project: Project, body: dict[str, Any], repo_path: str) -> list[Deployment]:
    pipeline_id = body.get("pipeline_id")
    if body.get("build_status") != "failed" or body.get("build_allow_failure") or not isinstance(pipeline_id, int):
        return []
    name = str(body.get("build_name") or "").strip() or None
    return deployments._record_job(session, project, _job_event(pipeline_id, name, repo_path))


def _record_deployment(session: Session, project: Project, body: dict[str, Any], repo_path: str,
                       received: datetime) -> list[Deployment]:
    state = _ENVIRONMENT_STATES.get(str(body.get("status") or ""))
    match = _COMMIT_IN_URL.search(str(body.get("commit_url") or ""))
    environment = str(body.get("environment") or "")
    if state is None or match is None or not environment:
        return []
    stamp = _ts(body.get("status_changed_at"))
    sent = stamp.isoformat() if stamp else None
    shaped = {
        "deployment": {"id": body.get("deployment_id"), "sha": match.group(1).lower(), "environment": environment},
        "deployment_status": {"state": state, "environment": environment, "target_url": body.get("deployable_url"),
                              "environment_url": body.get("environment_external_url"),
                              "log_url": None, "created_at": sent, "updated_at": sent},
        "workflow": {"path": _ci_config(body.get("project") or {})},
    }
    event = deployments._Event("deployment_status", shaped, repo_path)
    return deployments._record_status(session, project, event, received)


# ── The recorder handler ────────────────────────────────────────────────────────────────────────────

def handle(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """The `gitlab` recorder handler: record the deploy fact, then reconcile what it touched."""
    event = _stored_event(trigger, row.event)
    if event is None:
        return []
    name, repo_path = event.name, event.repo_path
    project = deployments._project(session, trigger.project_id)
    if project is None:
        return []
    received = row.received_at or deployments._now()
    trimmed = _trimmed(name, event.body)
    deployments._keep_trimmed(row, GITLAB_EVENT_HEADER, name, trimmed)
    if name == PUSH:
        touched = _record_push(session, project, trimmed, repo_path, row.dedup_key, received)
    elif name == PIPELINE:
        touched = _record_pipeline(session, project, trimmed, repo_path, received)
    elif name == JOB:
        touched = _record_job(session, project, trimmed, repo_path)
    else:
        touched = _record_deployment(session, project, trimmed, repo_path, received)
    return deployments.reconcile_all(session, touched)


def recorrelate(session: Session, now: datetime) -> None:
    """Re-run the `Deployment Hook` deliveries of the last `RECORRELATE_WINDOW` whose pipeline may have been recorded
    since (a matched one changes nothing: its state is already there). Only deliveries this handler recorded, of a
    trigger not paused: a stopped trigger's skipped delivery keeps its untrimmed preview and must record nothing."""
    rows = session.execute(
        select(TriggerDelivery, TaskTrigger)
        .join(TaskTrigger, TaskTrigger.id == TriggerDelivery.trigger_id)
        .where(TaskTrigger.source_kind == SOURCE_GITLAB, TaskTrigger.paused_at.is_(None),
               TriggerDelivery.state == FILTERED, TriggerDelivery.reason == REASON_RECORDED,
               TriggerDelivery.received_at >= now - deployments.RECORRELATE_WINDOW,
               TriggerDelivery.preview["text"].as_string().contains(f'"{GITLAB_EVENT_HEADER}": "{DEPLOYMENT}"'))
        .order_by(TriggerDelivery.received_at, TriggerDelivery.id)
        .limit(deployments.RECORRELATE_BATCH)
    ).all()
    deployments.recorrelate_deliveries(session, rows, _stored_event, _recorrelate_one,
                                       "production-qa: re-correlating GitLab delivery %s failed")


def _recorrelate_one(session: Session, project: Project, event: deployments._Event, row: TriggerDelivery) -> list[Deployment]:
    return _record_deployment(session, project, event.body, event.repo_path, row.received_at)


recorder.register(SOURCE_GITLAB, handle)
cron.register("gitlab_deploy_recorrelate", recorrelate)
