"""Azure DevOps deploy ingestion: the `azure_devops` recorder (record-only, architecture §5.1 for a second forge).

Vendor docs (read 2026-11-01): service hook events https://learn.microsoft.com/en-us/azure/devops/service-hooks/events,
the Web Hooks consumer https://learn.microsoft.com/en-us/azure/devops/service-hooks/consumers, the pipeline `Run`
resource https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/get?view=azure-devops-rest-7.1, the
release `Artifact` https://learn.microsoft.com/en-us/rest/api/azure/devops/release/releases/get-release?view=azure-devops-rest-7.1.

VERIFY: service hooks are not signed. The Web Hooks consumer sends either `basicAuthCredentials` (any username, the
trigger's secret as the password) or a custom `httpHeaders` line `X-Jaah-Token:<secret>`; either is compared in
constant time. Connect deploys shows the basic-auth form (header values are visible to anyone who can read the
subscription).

MAPPING — each event is translated into the GitHub-shaped event `deployments.py` already records, so the same
`deployments` rows, range and PRs, `deployed` edges and `deploy_failed` incidents follow (`deployments.reconcile`).
No Azure DevOps API is read (A-5): a fact the payload does not carry is not recorded.

| Service hook (`eventType`)                         | Recorded as                                              |
|----------------------------------------------------|----------------------------------------------------------|
| `git.push`, one `refs/heads/*` ref update          | `push`: `oldObjectId`→before, `newObjectId`→after, `commits[].commitId` |
| `ms.vss-pipelines.run-state-changed-event`         | `workflow_run` of the pipeline (`run.pipeline.name`)     |
| `ms.vss-pipelines.stage-state-changed-event`       | its run, then: the production stage → `deployment_status`; a failed stage → `workflow_job` failure (the stage is the failing job) |
| `ms.azure-devops-release.deployment-completed-event` | `workflow_run` of the release definition (one per environment deployment), its environment → `deployment_status`, a failure → `workflow_job` |

* Run: `run.id` (else the `/runs/<id>` of its URL); head commit and branch from the `Run` resource's
  `resources.repositories.self` (`version`, `refName`) — the service hook sample omits them, a run without them is
  not recorded. `state` `inProgress`/`canceling` → in progress; `completed` with `result` `succeeded` → success,
  `failed` → failure, `canceled` → cancelled. Azure DevOps names no trigger reason, so every run of a deploy
  pipeline reads as a `push` run (it may anchor). A rerun of failed stages keeps the run's first conclusion.
* Stage: the stage whose `name` or `displayName` is the project's production environment (case-insensitive) reports
  that environment; other stages report none (a build stage is not a deploy).
* Release: its row's run id is the release deployment's id negated (pipeline run ids count apart). Commit and
  branch from the first artifact (primary first) whose `definitionReference` `version`/
  `sourceVersion` id is a 40-hex commit (a Git artifact); a build artifact names a build id, so that release is not
  recorded. `status` `succeeded` → success, `failed`/`partiallySucceeded`/`rejected` → failure, `canceled` → cancelled.
* Repository: a push of another repository than the trigger's `source_config.repository` is ignored; pipeline and
  release events carry no repository id, so they are recorded under the trigger's repository when their pipeline or
  release definition is one of `projects.deploy_workflows` (case-insensitive; recorded under the project's spelling).

Dedup: the event's `id` (a retry resends it).
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.production_qa import Deployment
from app.models.projects import Project
from app.production_qa import deployments, recorder
from app.production_qa.adapters.common import lowered, text, web_url, with_secret
from app.production_qa.events import RaiseIntent
from app.releases import stages
from app.triggers import DATADOG_TOKEN_HEADER, SOURCE_AZURE_DEVOPS

EVENT_PUSH = "git.push"
EVENT_RUN = "ms.vss-pipelines.run-state-changed-event"
EVENT_STAGE = "ms.vss-pipelines.stage-state-changed-event"
EVENT_RELEASE = "ms.azure-devops-release.deployment-completed-event"
# What Connect deploys asks the owner to subscribe to, in this order.
EVENTS = (EVENT_PUSH, EVENT_RUN, EVENT_STAGE, EVENT_RELEASE)

_EVENT_HEADER = "x-azure-devops-event"
_RUN_IN_URL = re.compile(r"/runs/(\d+)|[?&]buildId=(\d+)")
_RUN_CONCLUSIONS = {"succeeded": "success", "failed": "failure", "canceled": "cancelled"}
_RELEASE_CONCLUSIONS = {"succeeded": "success", "failed": "failure", "partiallysucceeded": "failure",
                        "rejected": "failure", "canceled": "cancelled"}
_IN_PROGRESS = ("inprogress", "canceling", "running")


# ── Ingress ─────────────────────────────────────────────────────────────────────────────────────────

def _presented(headers: Mapping[str, str]) -> str:
    """The secret the request presents: `X-Jaah-Token`, else the password of `Authorization: Basic`."""
    named = lowered(headers)
    token = (named.get(DATADOG_TOKEN_HEADER) or "").strip()
    if token:
        return token
    scheme, _, value = (named.get("authorization") or "").strip().partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return ""
    return decoded.partition(":")[2]


def verify(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> bool:
    """Whether the request presents the trigger's secret (module docstring), in constant time. No secret: never."""
    return with_secret(trigger, lambda secret: bool(secret) and hmac.compare_digest(
        _presented(headers).encode(), secret.encode()))


def delivery_id(headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """The event's `id`, when the body is a JSON object carrying one."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return text(payload.get("id")) if isinstance(payload, dict) else None


# ── Field readers ───────────────────────────────────────────────────────────────────────────────────

def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _branch(ref: Any) -> Optional[str]:
    name = text(ref) or ""
    return name[len("refs/heads/"):] if name.startswith("refs/heads/") else None


def _link(node: dict[str, Any]) -> Optional[str]:
    return web_url(_dict(_dict(node.get("_links")).get("web")).get("href"))


def _run_id(run: dict[str, Any], *urls: Any) -> Optional[int]:
    if isinstance(run.get("id"), int) and not isinstance(run.get("id"), bool):
        return run["id"]
    for url in (run.get("url"), _link(run), *urls):
        match = _RUN_IN_URL.search(str(url or ""))
        if match:
            return int(match.group(1) or match.group(2))
    return None


def _same_repository(a: str, b: str) -> bool:
    """Two `organization/project/repo` names are one repository: case-insensitive, percent-decoded (a stored repo URL
    keeps `My%20Project`; a payload names `My Project`)."""
    return urllib.parse.unquote(a).lower() == urllib.parse.unquote(b).lower()


def _repo_path(repository: dict[str, Any]) -> Optional[str]:
    """A pushed repository's `organization/project/repo`: the project and repo by their `name`s, the organization from
    `remoteUrl` — its first path segment on dev.azure.com, its subdomain on `<org>.visualstudio.com` (a URL may carry
    a collection, or no project, where the project goes)."""
    parts = urllib.parse.urlsplit(str(repository.get("remoteUrl") or ""))
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if host == "dev.azure.com" and segments:
        org = segments[0]
    elif host.endswith(".visualstudio.com"):
        org = host[: -len(".visualstudio.com")]
    else:
        return None
    project, repo = text(_dict(repository.get("project")).get("name")), text(repository.get("name"))
    return f"{org}/{project}/{repo}" if project and repo else None


def _pipeline(project: Project, name: Optional[str]) -> Optional[str]:
    """The project's own spelling of a deploy pipeline `name` (Azure DevOps names are case-insensitive), else `name`."""
    if name is None:
        return None
    return next((w for w in project.deploy_workflows or [] if w.lower() == name.lower()), name)


def _environment(project: Project, *names: Any) -> Optional[str]:
    """The project's production environment when one of `names` is it (case-insensitive), else None."""
    production = (stages.production_environment(project) or "").strip()
    if not production:
        return None
    return production if any((text(n) or "").lower() == production.lower() for n in names) else None


# ── Translation into the GitHub-shaped events `deployments.py` records ──────────────────────────────

def _push(body: dict[str, Any], repo_path: str) -> list[deployments._Event]:
    resource = _dict(body.get("resource"))
    pushed = _repo_path(_dict(resource.get("repository")))
    if pushed is None or not _same_repository(pushed, repo_path):
        return []
    heads = [u for u in resource.get("refUpdates") or [] if isinstance(u, dict) and _branch(u.get("name"))]
    if len(heads) != 1:
        # The commit list is the whole push's: with several branches updated it names no one branch's history.
        return []
    update = heads[0]
    after = deployments._sha(update.get("newObjectId"))
    commits = [{"id": c.get("commitId")} for c in resource.get("commits") or [] if isinstance(c, dict)]
    return [deployments._Event(name="push", repo_path=repo_path, body={
        "ref": update.get("name"), "before": deployments._sha(update.get("oldObjectId")), "after": after,
        "forced": False, "commits": commits, "head_commit": {"id": after, "timestamp": resource.get("date")},
    })]


def _workflow_run(project: Project, run: dict[str, Any], pipeline: dict[str, Any], *, html_url: Optional[str],
                  repo_path: str) -> Optional[deployments._Event]:
    run_id = _run_id(run, html_url)
    name = _pipeline(project, text(_dict(run.get("pipeline")).get("name")) or text(pipeline.get("name")))
    repositories = _dict(_dict(run.get("resources")).get("repositories"))
    source = _dict(repositories.get("self")) or next((_dict(r) for r in repositories.values()), {})
    if run_id is None or name is None:
        return None
    state = (text(run.get("state")) or "").lower()
    conclusion = _RUN_CONCLUSIONS.get((text(run.get("result")) or "").lower()) if state == "completed" else None
    return deployments._Event(name="workflow_run", repo_path=repo_path, body={"workflow_run": {
        "id": run_id, "run_attempt": 1, "name": name, "path": name, "event": "push",
        "status": "completed" if conclusion else ("in_progress" if state in _IN_PROGRESS else "queued"),
        "conclusion": conclusion, "head_branch": _branch(source.get("refName")),
        "head_sha": deployments._sha(source.get("version")), "html_url": _link(run) or html_url,
        "run_started_at": run.get("createdDate"), "updated_at": run.get("finishedDate"),
    }})


def _status(run_id: int, environment: str, state: str, at: Any, repo_path: str) -> deployments._Event:
    return deployments._Event(name="deployment_status", repo_path=repo_path, body={
        "deployment": {"environment": environment},
        "deployment_status": {"state": state, "environment": environment, "created_at": at},
        "workflow_run": {"id": run_id, "run_attempt": 1},
    })


def _failed_job(run_id: int, name: str, repo_path: str) -> deployments._Event:
    return deployments._Event(name="workflow_job", repo_path=repo_path, body={"action": "completed", "workflow_job": {
        "run_id": run_id, "run_attempt": 1, "name": name, "conclusion": "failure", "steps": []}})


def _run_events(body: dict[str, Any], project: Project, repo_path: str) -> list[deployments._Event]:
    resource = _dict(body.get("resource"))
    run = _workflow_run(project, _dict(resource.get("run")), _dict(resource.get("pipeline")), html_url=None,
                        repo_path=repo_path)
    return [run] if run is not None else []


def _stage_events(body: dict[str, Any], project: Project, repo_path: str) -> list[deployments._Event]:
    resource = _dict(body.get("resource"))
    stage = _dict(resource.get("stage"))
    run = _workflow_run(project, _dict(resource.get("run")), _dict(resource.get("pipeline")), html_url=_link(stage),
                        repo_path=repo_path)
    if run is None:
        return []
    run_id = run.body["workflow_run"]["id"]
    out = [run]
    state, result = (text(stage.get("state")) or "").lower(), (text(stage.get("result")) or "").lower()
    stage_name = text(stage.get("displayName")) or text(stage.get("name")) or "stage"
    environment = _environment(project, stage.get("name"), stage.get("displayName"))
    reported = ("success" if result == "succeeded" else "failure" if result == "failed" else None
                ) if state == "completed" else ("in_progress" if state in _IN_PROGRESS else None)
    if environment and reported:
        out.append(_status(run_id, environment, reported, body.get("createdDate"), repo_path))
    if state == "completed" and result == "failed":
        out.append(_failed_job(run_id, stage_name, repo_path))
    return out


def _release_commit(release: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(commit, branch) of the release's first artifact that names a commit, primary first."""
    artifacts = [a for a in release.get("artifacts") or [] if isinstance(a, dict)]
    artifacts.sort(key=lambda a: not a.get("isPrimary"))
    for artifact in artifacts:
        reference = _dict(artifact.get("definitionReference"))
        commit = (deployments._sha(_dict(reference.get("sourceVersion")).get("id"))
                  or deployments._sha(_dict(reference.get("version")).get("id")))
        if commit:
            return commit, _branch(_dict(reference.get("branch")).get("id"))
    return None, None


def _release_events(body: dict[str, Any], project: Project, repo_path: str) -> list[deployments._Event]:
    deployment = _dict(_dict(body.get("resource")).get("deployment"))
    release = _dict(deployment.get("release"))
    definition = _pipeline(project, text(_dict(release.get("releaseDefinition")).get("name")) or text(
        _dict(deployment.get("releaseDefinition")).get("name")))
    environment = text(_dict(deployment.get("environment")).get("name")) or text(
        _dict(deployment.get("releaseEnvironment")).get("name"))
    commit, branch = _release_commit(release)
    if not isinstance(deployment.get("id"), int) or definition is None or environment is None or commit is None:
        return []
    # Negative: a release deployment's id counts apart from pipeline run ids, and both rows share one repo_path.
    deployment_id = -deployment["id"]
    status = (text(deployment.get("status")) or text(deployment.get("deploymentStatus")) or "").lower()
    conclusion = _RELEASE_CONCLUSIONS.get(status)
    finished = deployment.get("completedOn") or body.get("createdDate")
    run = deployments._Event(name="workflow_run", repo_path=repo_path, body={"workflow_run": {
        "id": deployment_id, "run_attempt": 1, "name": definition, "path": definition, "event": "push",
        "status": "completed" if conclusion else "in_progress", "conclusion": conclusion, "head_branch": branch,
        "head_sha": commit, "html_url": _link(release), "run_started_at": deployment.get("startedOn"),
        "updated_at": finished,
    }})
    out = [run]
    named = _environment(project, environment) or environment
    if conclusion in ("success", "failure"):
        out.append(_status(deployment_id, named, conclusion, finished, repo_path))
    if conclusion == "failure":
        out.append(_failed_job(deployment_id, environment, repo_path))
    return out


def translate(body: dict[str, Any], project: Project, repo_path: str) -> list[deployments._Event]:
    """The GitHub-shaped events one service hook delivery records, in order (module docstring)."""
    kind = text(body.get("eventType"))
    if kind == EVENT_PUSH:
        return _push(body, repo_path)
    if kind == EVENT_RUN:
        return _run_events(body, project, repo_path)
    if kind == EVENT_STAGE:
        return _stage_events(body, project, repo_path)
    if kind == EVENT_RELEASE:
        return _release_events(body, project, repo_path)
    return []


# ── The recorder handler ────────────────────────────────────────────────────────────────────────────

def _record(session: Session, project: Project, event: deployments._Event, row: TriggerDelivery,
            received: Any) -> list[Deployment]:
    if event.name == "push":
        return deployments._record_push(session, project, event, row.dedup_key, received)
    if event.name == "workflow_run":
        return deployments._record_run(session, project, event, received, forge=SOURCE_AZURE_DEVOPS)
    if event.name == "workflow_job":
        return deployments._record_job(session, project, event)
    return deployments._record_status(session, project, event, received)


def handle(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """The `azure_devops` recorder handler: translate, record each fact, then reconcile every run it touched once."""
    stored = row.event if isinstance(row.event, dict) else {}
    body = stored.get("body")
    repo_path = text((trigger.source_config or {}).get("repository"))
    project = deployments._project(session, trigger.project_id)
    if not isinstance(body, dict) or repo_path is None or project is None:
        return []
    events = translate(body, project, repo_path)
    deployments._keep_trimmed(row, _EVENT_HEADER, text(body.get("eventType")) or "", {
        "eventType": body.get("eventType"), "id": body.get("id"),
        "events": [{"name": e.name, "body": e.body} for e in events]})
    received = row.received_at or deployments._now()
    touched: dict[int, Deployment] = {}
    for event in events:
        for deployment in _record(session, project, event, row, received):
            touched[deployment.id] = deployment
    return deployments.reconcile_all(session, touched.values())


recorder.register(SOURCE_AZURE_DEVOPS, handle)
