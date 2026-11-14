"""Deploy ingestion (architecture §5.1, A-3/A-4/A-5/A-15/A-16): the push ledger, run/job/status events, classification,
range and PRs, and `reconcile`.

WHERE THE FACTS COME FROM — an `azure_devops` trigger (`azure_devops.py` maps each service hook onto the GitHub-shaped
events below and records them through the same functions), or a `record_only` `github` trigger (Connect deploys creates two: *Deploys* for `push`,
`workflow_run`, `deployment_status`; *Deploy jobs* for `workflow_job`). `handle` is the recorder handler of the
`github` kind; it acts only for a record-only trigger, and only on an event its config lists for the repository it
names. No forge API is ever read here (A-5, R2): a range is walked over recorded pushes, PRs come from merge SHAs the
control plane already holds. Bitbucket's pushes and build statuses map onto the same rows and the same `reconcile` in
`bitbucket.py`.

* `push` → one `branch_pushes` row (a branch push; tags and deletions are not a deploy's history). The delivery's
  stored event is trimmed to the fields the ledger reads.
* `workflow_run` → one `deployments` row per run attempt of a workflow in `projects.deploy_workflows` (A-3), or of a
  push run on the branch of a release stage still learning its environment (`app/releases/anchoring.py`).
* `workflow_job` completed `failure` → the run's failing job and its first failing step (first one reported wins).
* `deployment_status` → one `deployment_environments` row; a successful one's `environment_url` is its stage's
  address (`anchoring.record_url`). One that no recorded push run carries settles releases itself
  (`anchoring.on_status`: a Pages or other non-push deploy). Correlated by `workflow_run.id` when the payload carries
  it, else the run id in `target_url`/`log_url` (`…/actions/runs/<run>/job/<job>`, pinned by recorded Jaah Deploy
  statuses in `tests/fixtures/production_qa/`), else `(deployment.sha, workflow path)`. A status or job event whose
  run is not recorded yet is re-correlated by the cron for `RECORRELATE_WINDOW`, from the trimmed copy kept in its
  delivery's preview (intake drops a settled delivery's event body).

CLASSIFICATION (A-4), recomputed by every `reconcile`:
* `shipped` — a `deployment_status` `success` arrived for some environment.
* `status` — the conclusion always wins: failure/timed_out/startup_failure → `failure`; cancelled/stale →
  `cancelled`; success → `success` when shipped, else `skipped` (Jaah's superseded and held runs conclude success
  and deploy nothing); any other conclusion → `skipped`. No conclusion yet: `queued`/`in_progress`.
* `environment` — production when shipped there, else the first environment shipped that this workflow ships from
  no other branch (Jaah's develop runs also ship `jaahd-publish`, which master runs ship too), else the first one
  shipped, else the latest one reported,
  else the one last recorded for the same `(workflow_path, head_branch)`, else `unknown`.
* anchor — shipped to the project's production environment (`releases.stages.production_environment`) AND
  `run_event == 'push'`. A `workflow_dispatch` run
  never anchors (it may ship a pinned image or only plan).

RANGE (anchors, and failed push runs for their incident's facts): from the head of the anchor of the same
`(project, repo, environment)` that finished last before this run (finish order, so a late push delivery reorders
nothing), walk `branch_pushes` backward from the push whose `after` is the head (`MAX_HOPS`). `first` (no earlier
anchor: the head push's commits), `resolved`, `rollback` (the head already shipped in an anchor that finished
earlier: no PRs, no edges),
`partial` + note (force push, truncated push, broken chain, hop cap, head push never seen), `pending` (head push not
seen yet; `partial` after `PENDING_FOR`). PRs: `merge_lane_rows.merge_sha` ∪ `task_pr_changes.merge_sha` in the range.

RELEASES: every reconcile lets a learning stage learn from the run (`anchoring.learn`, then re-classifies); the
effects below anchor the releases the run carries (`anchoring.anchor`). A failed run raises an incident only for a
watched workflow (Bitbucket: every recorded one): a learning run is recorded, never an incident.

EFFECTS, run when the reconcile's outputs hash differs from `reconciled_hash` (each also safe to repeat):
`edges.on_deployment` (a `deployed` edge per PR task of an anchor — the survival clock's start), a `deploy_failed`
incident for a failure once its failing job is known or `FAILURE_GRACE` after it finished (any environment, D5), the
resolve of that workflow/environment's open `deploy_failed` incidents by the next successful anchor, and the
linkage/regression/probes hooks for anchors. Measurement ignores the Production QA gate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Callable, Collection, Iterable
from typing import Any, Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.merge_lane import MergeLaneRow
from app.models.production_qa import (
    BranchPush,
    Deployment,
    DeploymentCommit,
    DeploymentEnvironment,
    DeploymentPr,
    Incident,
)
from app.models.projects import Project
from app.models.task_extras import TaskPrChanges
from app.models.tasks import Task
from app.production_qa import cron, edges, incidents, linkage, probes, recorder, regression
from app.production_qa.events import RaiseIntent
from app.releases import anchoring, stages
from app.triggers import SOURCE_BITBUCKET, SOURCE_GITHUB
from app.clock import parse_utc, utcnow

log = logging.getLogger("cockpit-production-qa")

# The longest walk back through `branch_pushes` one range takes.
MAX_HOPS = 500
# GitHub lists at most this many commits in one `push` payload; a push that lists this many may hold more.
PUSH_COMMIT_CAP = 2048
# A range whose head push is unseen stays `pending` this long after the run finished, then reads `partial`.
PENDING_FOR = timedelta(minutes=10)
# A `partial` range is recomputed (every `PARTIAL_EVERY_MINUTES`) this long after its run finished.
PARTIAL_RECOMPUTE_FOR = timedelta(hours=24)
PARTIAL_EVERY_MINUTES = 10
# A failure with no failing job reported yet is recorded this long after it finished, as job `run`.
FAILURE_GRACE = timedelta(minutes=2)
# How long an unmatched `deployment_status`/`workflow_job` delivery is re-correlated.
RECORRELATE_WINDOW = timedelta(minutes=30)
# `branch_pushes` are kept this long (A-15); an anchor whose range reaches further back reads `partial`.
PUSH_RETENTION = timedelta(days=180)
_RETENTION_BATCH = 5000

_EVENT_PUSH = "push"
_EVENT_RUN = "workflow_run"
_EVENT_JOB = "workflow_job"
_EVENT_STATUS = "deployment_status"
_DEPLOY_EVENTS = (_EVENT_PUSH, _EVENT_RUN, _EVENT_JOB, _EVENT_STATUS)

_FAILED = ("failure", "timed_out", "startup_failure")
_CANCELLED = ("cancelled", "stale")
_ZERO_SHA = "0" * 40
_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_IN_URL = re.compile(r"/actions/runs/(\d+)(?:/attempts/(\d+))?")
_PR_URL = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", re.IGNORECASE)
# A GitLab merge request (gitlab.com or self-managed, a sub-path install included): the path before `/-/`.
_MR_URL = re.compile(r"^https?://[^/]+/(.+?)/-/merge_requests/(\d+)", re.IGNORECASE)
# A terminal `deployment_status` state is never overwritten by an earlier-sent progress state; `success` is never
# overwritten at all — GitHub later marks a superseded deployment `inactive`, and having shipped is history.
_TERMINAL_STATES = ("success", "failure", "error", "inactive")
# A push re-reconciles at most this many waiting ranges of its repo (the cron takes the rest).
_PUSH_RECONCILES = 50
# The re-correlation sweep reads at most this many deliveries per pass.
RECORRELATE_BATCH = 2000


_now = utcnow  # bound per module, so a test freezes this clock without the others'


def _project(session: Session, project_id: int) -> Optional[Project]:
    return session.scalar(select(Project).where(Project.id == project_id))


def _sha(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if _SHA.fullmatch(text) else None


def _workflow_path(value: Any) -> Optional[str]:
    """A run's workflow path without a `@ref` suffix (a called or dynamic workflow names one)."""
    text = str(value or "").strip()
    return text.split("@", 1)[0] or None


def _watched(project: Project, path: Optional[str]) -> bool:
    """Whether `path` is one of the project's deploy workflows; a bare file name matches its `.github/workflows/`
    path, the way the settings field accepts `deploy.yml`."""
    if not path:
        return False
    for watched in project.deploy_workflows or []:
        if path == watched or path == f".github/workflows/{watched}" or path.rsplit("/", 1)[-1] == watched:
            return True
    return False


# ── The recorder handler ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Event:
    name: str
    body: dict[str, Any]
    repo_path: str
    config: dict[str, Any] = field(default_factory=dict)  # the trigger's `source_config`


def _repository_full_name(body: dict[str, Any]) -> Any:
    return (body.get("repository") or {}).get("full_name")


def stored_event(trigger: TaskTrigger, stored: Any, *, source_kind: str, header: str,
                 repo_of: Callable[[dict[str, Any]], Any] = _repository_full_name,
                 names: Optional[Collection[str]] = None, require_named: bool = False) -> Optional[_Event]:
    """The deploy event a delivery's stored `{headers, body}` carries (every forge's recorder parses one), or None:
    another kind than `source_kind`, not a record-only trigger, an event not in `names` (any, when None) or one its
    config does not list, or another repository than the one it names (`repo_of(body)`, case-insensitive). With
    `require_named`, a config naming no repository matches none (GitLab's and Bitbucket's always name one,
    `trigger_config`); without it, it matches every repository. Header keys are compared lower-cased."""
    if trigger.source_kind != source_kind or not recorder.records_only(trigger) or not isinstance(stored, dict):
        return None
    headers = {str(k).lower(): v for k, v in (stored.get("headers") or {}).items()}
    body = stored.get("body")
    name = str(headers.get(header) or "")
    config = trigger.source_config or {}
    if (names is not None and name not in names) or name not in (config.get("events") or []) \
            or not isinstance(body, dict):
        return None
    repo_path = str(repo_of(body) or "").strip()
    named = str(config.get("repository") or "").strip()
    if not repo_path or (not named and require_named) or (named and named.lower() != repo_path.lower()):
        return None
    return _Event(name=name, body=body, repo_path=repo_path, config=config)


def _event_of(trigger: TaskTrigger, stored: Any) -> Optional[_Event]:
    """The GitHub deploy event a stored delivery carries (`stored_event`); a config naming no repository matches every
    one."""
    return stored_event(trigger, stored, source_kind=SOURCE_GITHUB, header="x-github-event", names=_DEPLOY_EVENTS)


def handle(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """The `github` recorder handler: record the deploy fact, then reconcile what it touched."""
    event = _event_of(trigger, row.event)
    if event is None:
        return []
    project = _project(session, trigger.project_id)
    if project is None:
        return []
    received = row.received_at or _now()
    _keep_trimmed(row, "x-github-event", event.name, _trimmed(event))
    if event.name == _EVENT_PUSH:
        touched = _record_push(session, project, event, row.dedup_key, received)
    elif event.name == _EVENT_RUN:
        touched = _record_run(session, project, event, received)
    elif event.name == _EVENT_JOB:
        touched = _record_job(session, project, event)
    else:
        touched = _record_status(session, project, event, received)
    intents = reconcile_all(session, touched)
    if event.name == _EVENT_STATUS:
        _anchor_status(session, project, event)
    return intents


def _anchor_status(session: Session, project: Project, event: _Event) -> None:
    """Let a `deployment_status` settle the releases it carries (`anchoring.on_status`), unless a push run the ledger
    records carries it for this environment: that run anchors them, as it always has (`reconcile`) — recorded already,
    or still to be (its status can arrive before its run)."""
    status, deployment = event.body.get("deployment_status") or {}, event.body.get("deployment") or {}
    environment = str(status.get("environment") or deployment.get("environment") or "")
    sha = _sha(deployment.get("sha"))
    if not environment or sha is None:
        return
    run = _status_run(session, project, event)
    if run is not None and run.run_event == "push" and run.environment in (environment, None, "unknown"):
        return
    raw_run = event.body.get("workflow_run")
    payload_run: dict[str, Any] = raw_run if isinstance(raw_run, dict) else {}
    if run is None and payload_run.get("event") == "push" and (
            _watched(project, _workflow_path(payload_run.get("path") or (event.body.get("workflow") or {}).get("path")))
            or anchoring.learning(session, project, payload_run.get("head_branch"), "push")):
        return
    # Only a run page of this repository: the console's Open run button opens it.
    link = next((u for u in (status.get("log_url"), status.get("target_url")) if isinstance(u, str)
                 and u.startswith(f"https://github.com/{event.repo_path}/actions/runs/")), None)
    anchoring.on_status(session, project, environment=environment, sha=sha, ref=deployment.get("ref"),
                        state=str(status.get("state") or ""), url=status.get("environment_url"), link=link,
                        deployment=run, now=_now())


def reconcile_all(session: Session, touched: Iterable[Deployment]) -> list[RaiseIntent]:
    """Every recorder handler's tail: reconcile each deployment a delivery touched, the intents in that order."""
    return [intent for deployment in touched for intent in reconcile(session, deployment)]


def _trimmed(event: _Event) -> dict[str, Any]:
    """Only the fields this module reads of each event (§5.1 step 2)."""
    body, repo = event.body, {"full_name": event.repo_path}
    if event.name == _EVENT_PUSH:
        head = body.get("head_commit") or {}
        return {"ref": body.get("ref"), "before": body.get("before"), "after": body.get("after"),
                "forced": bool(body.get("forced")), "repository": repo,
                "commits": [{"id": c.get("id")} for c in body.get("commits") or [] if isinstance(c, dict)],
                "head_commit": {"id": head.get("id"), "timestamp": head.get("timestamp")} if head else None}
    if event.name == _EVENT_RUN:
        run = body.get("workflow_run") or {}
        return {"action": body.get("action"), "repository": repo, "workflow_run": {k: run.get(k) for k in (
            "id", "run_attempt", "name", "path", "event", "status", "conclusion", "head_branch", "head_sha",
            "html_url", "run_started_at", "updated_at")}}
    if event.name == _EVENT_JOB:
        job = body.get("workflow_job") or {}
        return {"action": body.get("action"), "repository": repo, "workflow_job": {
            **{k: job.get(k) for k in ("run_id", "run_attempt", "name", "conclusion")},
            "steps": [{k: s.get(k) for k in ("name", "number", "conclusion")}
                      for s in job.get("steps") or [] if isinstance(s, dict)]}}
    deployment, status = body.get("deployment") or {}, body.get("deployment_status") or {}
    out: dict[str, Any] = {
        "action": body.get("action"), "repository": repo,
        "deployment": {k: deployment.get(k) for k in ("id", "sha", "ref", "environment")},
        "deployment_status": {k: status.get(k) for k in (
            "state", "environment", "environment_url", "target_url", "log_url", "created_at", "updated_at")},
    }
    if isinstance(body.get("workflow"), dict):
        out["workflow"] = {"path": body["workflow"].get("path")}
    if isinstance(body.get("workflow_run"), dict):
        out["workflow_run"] = {k: body["workflow_run"].get(k) for k in ("id", "run_attempt")}
    return out


def _keep_trimmed(row: TriggerDelivery, header: str, name: str, body: dict[str, Any]) -> None:
    """Keep only what a forge's recorder read of the delivery (`body`, its trimmed event under the forge's event
    `header`), and keep it in the preview: intake drops the event body of a settled delivery, and the preview is what
    each forge's re-correlation sweep reads back."""
    from app.trigger_intake import preview_of

    row.event = {"headers": {header: name}, "body": body}
    row.preview = preview_of(row.event)


def _record_push(session: Session, project: Project, event: _Event, gh_delivery: Optional[str],
                 received: datetime, truncated: Optional[bool] = None) -> list[Deployment]:
    """`truncated`: whether the push lists fewer commits than it carried (a forge that says so, GitLab); unset, a
    push listing `PUSH_COMMIT_CAP` commits may hold more."""
    body = event.body
    ref = str(body.get("ref") or "")
    before, after = _sha(body.get("before")), _sha(body.get("after"))
    if not ref.startswith("refs/heads/") or before is None or after is None or after == _ZERO_SHA:
        return []
    shas = [s for s in (_sha(c.get("id")) for c in body.get("commits") or [] if isinstance(c, dict)) if s]
    session.execute(
        pg_insert(BranchPush)
        .values(
            org_id=project.org_id, project_id=project.id, repo_path=event.repo_path, branch=ref[len("refs/heads/"):],
            before_sha=before, after_sha=after, forced=bool(body.get("forced")), commit_shas=shas[:PUSH_COMMIT_CAP],
            truncated=len(shas) >= PUSH_COMMIT_CAP if truncated is None else truncated, gh_delivery=gh_delivery,
            pushed_at=parse_utc((body.get("head_commit") or {}).get("timestamp")) or received, received_at=received,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "repo_path", "branch", "before_sha", "after_sha"])
    )
    session.flush()
    return ranges_waiting(session, project, event.repo_path, received)


def ranges_waiting(session: Session, project: Project, repo_path: str, received: datetime) -> list[Deployment]:
    """The recent ranges of `repo_path` a push just recorded may resolve: locked (the cron reconciles the same rows),
    skipping one the cron holds — the cron's next pending pass, or partial pass (`PARTIAL_EVERY_MINUTES`), takes it."""
    return list(session.scalars(
        select(Deployment).where(
            Deployment.project_id == project.id, Deployment.repo_path == repo_path,
            Deployment.range_status.in_(("pending", "partial")),
            func.coalesce(Deployment.finished_at, Deployment.last_received_at) > received - PARTIAL_RECOMPUTE_FOR,
        ).order_by(Deployment.id.desc()).limit(_PUSH_RECONCILES)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    ))


def _locked_run(session: Session, project_id: int, repo_path: str, run_id: int,
                run_attempt: int) -> Optional[Deployment]:
    return session.scalar(
        select(Deployment)
        .where(Deployment.project_id == project_id, Deployment.repo_path == repo_path,
               Deployment.run_id == run_id, Deployment.run_attempt == run_attempt)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _record_run(session: Session, project: Project, event: _Event, received: datetime,
                forge: str = SOURCE_GITHUB) -> list[Deployment]:
    run = event.body.get("workflow_run") or {}
    path = _workflow_path(run.get("path"))
    head_sha = _sha(run.get("head_sha"))
    if head_sha is None or not isinstance(run.get("id"), int) or not (
            _watched(project, path) or anchoring.learning(session, project, run.get("head_branch"), run.get("event"))):
        return []
    run_attempt = int(run.get("run_attempt") or 1)
    session.execute(
        pg_insert(Deployment)
        .values(
            org_id=project.org_id, project_id=project.id, repo_path=event.repo_path, forge=forge, workflow_path=path,
            run_id=run["id"], run_attempt=run_attempt, head_sha=head_sha, status="queued",
            first_received_at=received, last_received_at=received,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "repo_path", "run_id", "run_attempt"])
    )
    deployment = _locked_run(session, project.id, event.repo_path, run["id"], run_attempt)
    if deployment is None:
        return []
    deployment.workflow_name = run.get("name") or deployment.workflow_name
    deployment.run_event = run.get("event") or deployment.run_event
    deployment.run_url = run.get("html_url") or deployment.run_url
    deployment.head_branch = run.get("head_branch") or deployment.head_branch
    deployment.started_at = parse_utc(run.get("run_started_at")) or deployment.started_at
    deployment.last_received_at = max(deployment.last_received_at, received)
    if deployment.conclusion_raw is None:
        # A completed run's conclusion is final; an earlier-sent progress event arriving late changes nothing.
        if run.get("status") == "completed" and run.get("conclusion"):
            deployment.conclusion_raw = str(run["conclusion"])
            deployment.finished_at = parse_utc(run.get("updated_at")) or received
        elif run.get("status") in ("in_progress", "queued", "waiting", "requested", "pending"):
            deployment.status = "in_progress" if run.get("status") == "in_progress" else "queued"
    session.flush()
    return [deployment]


def _record_job(session: Session, project: Project, event: _Event) -> list[Deployment]:
    job = event.body.get("workflow_job") or {}
    if event.body.get("action") != "completed" or job.get("conclusion") not in _FAILED:
        return []
    run_id = job.get("run_id")
    if not isinstance(run_id, int):
        return []
    deployment = _locked_run(session, project.id, event.repo_path, run_id, int(job.get("run_attempt") or 1))
    if deployment is None or deployment.failing_job is not None:
        return []
    deployment.failing_job = str(job.get("name") or "").strip() or None
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict) and s.get("conclusion") in _FAILED]
    steps.sort(key=lambda s: s.get("number") or 0)
    deployment.failing_step = str(steps[0].get("name") or "") or None if steps else None
    session.flush()
    return [deployment]


def _status_run(session: Session, project: Project, event: _Event) -> Optional[Deployment]:
    """The run attempt a `deployment_status` belongs to (module docstring), locked; None while unmatched."""
    body = event.body
    status = body.get("deployment_status") or {}
    run = body.get("workflow_run") or {}
    run_id: Optional[int] = run.get("id") if isinstance(run.get("id"), int) else None
    attempt: Optional[int] = int(run["run_attempt"]) if isinstance(run.get("run_attempt"), int) else None
    if run_id is None:
        for url in (status.get("target_url"), status.get("log_url")):
            match = _RUN_IN_URL.search(str(url or ""))
            if match:
                run_id = int(match.group(1))
                attempt = int(match.group(2)) if match.group(2) else attempt
                break
    stmt = select(Deployment).where(Deployment.project_id == project.id, Deployment.repo_path == event.repo_path)
    if run_id is not None:
        stmt = stmt.where(Deployment.run_id == run_id)
        if attempt is not None:
            stmt = stmt.where(Deployment.run_attempt == attempt)
    else:
        head_sha = _sha((body.get("deployment") or {}).get("sha"))
        path = _workflow_path((body.get("workflow") or {}).get("path"))
        if head_sha is None:
            return None
        stmt = stmt.where(Deployment.head_sha == head_sha)
        if path:
            stmt = stmt.where(Deployment.workflow_path == path)
    found = session.scalar(stmt.order_by(Deployment.run_attempt.desc(), Deployment.id.desc()).limit(1))
    if found is None:
        return None
    return _locked_run(session, found.project_id, found.repo_path, found.run_id, found.run_attempt)


def _record_status(session: Session, project: Project, event: _Event, received: datetime) -> list[Deployment]:
    status = event.body.get("deployment_status") or {}
    environment = str(status.get("environment") or (event.body.get("deployment") or {}).get("environment") or "")
    state = str(status.get("state") or "")
    if not environment or not state:
        return []
    deployment = _status_run(session, project, event)
    if deployment is None:
        return []
    anchoring.record_url(session, project, deployment, environment, state, status.get("environment_url"))
    sent = parse_utc(status.get("updated_at")) or parse_utc(status.get("created_at")) or received
    current, created = _environment_row(session, deployment, environment, state, sent)
    if not created:
        if current.state == state or current.state == "success" or (state != "success" and (
                (current.state in _TERMINAL_STATES and state not in _TERMINAL_STATES) or sent < current.received_at)):
            # A `success` always lands (a retried one arriving after the next deploy's `inactive` included).
            # A progress state, or an older one, never replaces what was reported after it.
            return []
        current.state, current.received_at = state, sent
    deployment.last_received_at = max(deployment.last_received_at, received)
    session.flush()
    return [deployment]


def _environment_row(session: Session, deployment: Deployment, environment: str, state: str,
                     sent: datetime) -> tuple[DeploymentEnvironment, bool]:
    """The deployment's row for `environment` and whether this call added it as a first report (`state` as of
    `sent`): each forge decides for itself whether a later report replaces an existing row's state."""
    current = session.scalar(select(DeploymentEnvironment).where(
        DeploymentEnvironment.deployment_id == deployment.id, DeploymentEnvironment.environment == environment))
    if current is not None:
        return current, False
    row = DeploymentEnvironment(org_id=deployment.org_id, deployment_id=deployment.id,
                                environment=environment, state=state, received_at=sent)
    session.add(row)
    return row, True


# ── Classification, range, PRs ──────────────────────────────────────────────────────────────────────

def _classify(session: Session, deployment: Deployment, project: Project) -> None:
    envs = list(session.scalars(
        select(DeploymentEnvironment).where(DeploymentEnvironment.deployment_id == deployment.id)
        .order_by(DeploymentEnvironment.received_at, DeploymentEnvironment.environment)
    ))
    shipped = [e.environment for e in envs if e.state == "success"]
    production = stages.production_environment(project, session)
    if production and production in shipped:
        environment = production
    elif shipped:
        # A run that ships to several environments (Jaah: `dev` plus the package environment `jaahd-publish`) is
        # named for the one this workflow ships only from this branch; the shared ones are its side jobs.
        shared = set(session.scalars(
            select(DeploymentEnvironment.environment).distinct()
            .join(Deployment, Deployment.id == DeploymentEnvironment.deployment_id)
            .where(Deployment.project_id == deployment.project_id,
                   Deployment.workflow_path == deployment.workflow_path,
                   Deployment.head_branch != deployment.head_branch,
                   DeploymentEnvironment.environment.in_(shipped), DeploymentEnvironment.state == "success")
        )) if deployment.head_branch else set()
        environment = next((e for e in shipped if e not in shared), shipped[0])
    elif envs:
        environment = envs[-1].environment
    else:
        environment = session.scalar(
            select(Deployment.environment).where(
                Deployment.project_id == deployment.project_id, Deployment.workflow_path == deployment.workflow_path,
                Deployment.head_branch == deployment.head_branch, Deployment.id != deployment.id,
                Deployment.environment != "unknown",
            ).order_by(Deployment.id.desc()).limit(1)
        ) or "unknown"
    deployment.shipped = bool(shipped)
    deployment.environment = environment
    deployment.is_production = bool(production) and environment == production
    conclusion = deployment.conclusion_raw
    if conclusion is not None:
        if conclusion in _FAILED:
            deployment.status = "failure"
        elif conclusion in _CANCELLED:
            deployment.status = "cancelled"
        elif conclusion == "success" and deployment.shipped:
            deployment.status = "success"
        else:
            deployment.status = "skipped"
    deployment.is_anchor = (
        bool(production) and production in shipped and deployment.run_event == "push"
    )


@dataclass
class _Range:
    status: Optional[str] = None
    from_sha: Optional[str] = None
    note: Optional[str] = None
    commits: tuple[str, ...] = ()


def _head_push(session: Session, deployment: Deployment, sha: str) -> Optional[BranchPush]:
    stmt = select(BranchPush).where(
        BranchPush.project_id == deployment.project_id, BranchPush.repo_path == deployment.repo_path,
        BranchPush.after_sha == sha,
    )
    if deployment.head_branch:
        stmt = stmt.where(BranchPush.branch == deployment.head_branch)
    return session.scalar(stmt.order_by(BranchPush.id).limit(1))


def _earlier_anchor(session: Session, deployment: Deployment) -> Optional[Deployment]:
    """The anchor of the same environment that finished last before this one: where its range starts. Finish order,
    not push-arrival order, so a head push delivered late never reorders two deploys."""
    finished = deployment.finished_at or deployment.last_received_at
    return session.scalar(
        select(Deployment).where(
            Deployment.project_id == deployment.project_id, Deployment.repo_path == deployment.repo_path,
            Deployment.environment == deployment.environment, Deployment.is_anchor.is_(True),
            Deployment.id != deployment.id,
            func.coalesce(Deployment.finished_at, Deployment.last_received_at) <= finished,
        ).order_by(func.coalesce(Deployment.finished_at, Deployment.last_received_at).desc(), Deployment.id.desc())
        .limit(1)
    )


def _range(session: Session, deployment: Deployment, now: datetime) -> _Range:
    earlier = _earlier_anchor(session, deployment)
    if earlier is not None and earlier.head_sha == deployment.head_sha:
        return _Range("resolved", from_sha=earlier.head_sha, note="redeploy of the same commit")
    shipped_before = session.scalar(
        select(DeploymentCommit.deployment_id)
        .join(Deployment, Deployment.id == DeploymentCommit.deployment_id)
        .where(DeploymentCommit.sha == deployment.head_sha, Deployment.id != deployment.id,
               Deployment.project_id == deployment.project_id, Deployment.repo_path == deployment.repo_path,
               Deployment.environment == deployment.environment, Deployment.is_anchor.is_(True),
               func.coalesce(Deployment.finished_at, Deployment.last_received_at)
               <= (deployment.finished_at or deployment.last_received_at))
        .limit(1)
    )
    if shipped_before is not None:
        return _Range("rollback", from_sha=earlier.head_sha if earlier else None,
                      note=f"head already shipped in deploy #{shipped_before}")
    head = _head_push(session, deployment, deployment.head_sha)
    if head is None:
        finished = deployment.finished_at or deployment.last_received_at
        if finished is not None and now - finished >= PENDING_FOR:
            return _Range("partial", note="head push not recorded")
        return _Range("pending")
    previous = earlier
    if previous is None:
        return _Range("first", commits=tuple(head.commit_shas or ()) or (deployment.head_sha,))
    since = previous.head_sha
    collected: list[str] = []
    push: Optional[BranchPush] = head
    hops = 0
    while push is not None:
        hops += 1
        shas = list(push.commit_shas or ())
        if since in shas:
            collected[:0] = shas[shas.index(since) + 1:]
            return _Range("resolved", from_sha=since, commits=tuple(collected))
        collected[:0] = shas
        if push.before_sha == since:
            return _Range("resolved", from_sha=since, commits=tuple(collected))
        if push.forced:
            return _Range("partial", from_sha=since, note=f"force push at {push.after_sha[:7]}",
                          commits=tuple(collected))
        if push.truncated:
            return _Range("partial", from_sha=since, note=f"push at {push.after_sha[:7]} listed too many commits",
                          commits=tuple(collected))
        if hops >= MAX_HOPS:
            return _Range("partial", from_sha=since, note=f"stopped after {MAX_HOPS} pushes",
                          commits=tuple(collected))
        push = session.scalar(
            select(BranchPush).where(
                BranchPush.project_id == deployment.project_id, BranchPush.repo_path == deployment.repo_path,
                BranchPush.branch == push.branch, BranchPush.after_sha == push.before_sha,
            ).order_by(BranchPush.id).limit(1)
        )
    return _Range("partial", from_sha=since, note="a push between the two deploys was not recorded",
                  commits=tuple(collected))


def _repo_of(pr_url: Optional[str], fallback: str) -> str:
    match = _PR_URL.search(pr_url or "")
    if match:
        return match.group(1)
    match = _MR_URL.search(pr_url or "")
    if match:
        # A self-managed install's sub-path precedes the project path: the deployed repository, when it ends so.
        path = match.group(1)
        return fallback if (path.lower() == fallback.lower() or path.lower().endswith("/" + fallback.lower())) else path
    return fallback


def _prs(session: Session, deployment: Deployment, commits: tuple[str, ...]) -> list[tuple[str, int, Optional[int], str, str]]:
    """`(repo_path, pr_number, task_id, merge_sha, source)` per PR whose merge SHA is in `commits`."""
    if not commits:
        return []
    found: dict[tuple[str, int], tuple[str, int, Optional[int], str, str]] = {}
    for pr_number, pr_url, task_id, merge_sha in session.execute(
        select(MergeLaneRow.pr_number, MergeLaneRow.pr_url, MergeLaneRow.task_id, MergeLaneRow.merge_sha)
        .where(MergeLaneRow.project_id == deployment.project_id, MergeLaneRow.merge_sha.in_(commits))
        .order_by(MergeLaneRow.id)
    ):
        repo = _repo_of(pr_url, deployment.repo_path)
        found.setdefault((repo.lower(), pr_number), (repo, pr_number, task_id, merge_sha, "merge_lane"))
    for pr_number, pr_url, task_id, merge_sha in session.execute(
        select(TaskPrChanges.pr_number, TaskPrChanges.pr_url, TaskPrChanges.task_id, TaskPrChanges.merge_sha)
        .join(Task, Task.id == TaskPrChanges.task_id)
        .where(Task.project_id == deployment.project_id, TaskPrChanges.merge_sha.in_(commits),
               TaskPrChanges.pr_number > 0)
        .order_by(TaskPrChanges.task_id)
    ):
        repo = _repo_of(pr_url, deployment.repo_path)
        found.setdefault((repo.lower(), pr_number), (repo, pr_number, task_id, merge_sha, "pr_changes"))
    # Only PRs of the deployed repository.
    return sorted(v for k, v in found.items() if k[0] == deployment.repo_path.lower())


def _replace_children(session: Session, deployment: Deployment, commits: tuple[str, ...],
                      prs: list[tuple[str, int, Optional[int], str, str]]) -> None:
    session.execute(delete(DeploymentCommit).where(DeploymentCommit.deployment_id == deployment.id))
    session.execute(delete(DeploymentPr).where(DeploymentPr.deployment_id == deployment.id))
    for sha in dict.fromkeys(commits):
        session.add(DeploymentCommit(org_id=deployment.org_id, deployment_id=deployment.id, sha=sha))
    for repo, number, task_id, merge_sha, source in prs:
        session.add(DeploymentPr(org_id=deployment.org_id, deployment_id=deployment.id, repo_path=repo,
                                 pr_number=number, task_id=task_id, merge_sha=merge_sha, source=source))
    session.flush()


# ── reconcile ───────────────────────────────────────────────────────────────────────────────────────

def failure_due(deployment: Deployment, now: datetime) -> bool:
    """A failure is recorded once its failing job is known, or `FAILURE_GRACE` after it finished."""
    if deployment.status != "failure":
        return False
    if deployment.failing_job:
        return True
    finished = deployment.finished_at or deployment.last_received_at
    return finished is not None and now - finished >= FAILURE_GRACE


def reconcile(session: Session, deployment: Deployment) -> list[RaiseIntent]:
    """Recompute classification, range and PRs, idempotently, and run the keyed effects when the outputs changed
    (module docstring). Flushes, never commits. Reads the clock (`_now`) for the pending and failure-grace windows."""
    now = _now()
    project = _project(session, deployment.project_id)
    if project is None:
        return []
    _classify(session, deployment, project)
    if anchoring.learn(session, project, deployment, now):
        _classify(session, deployment, project)
    # Anchors carry a range; so does a failed push run, for its incident's facts and suspects (never an edge).
    ranged = deployment.is_anchor or (deployment.status == "failure" and deployment.run_event == "push")
    span = _range(session, deployment, now) if ranged else _Range()
    commits = span.commits if span.status not in (None, "pending", "rollback") else ()
    prs = _prs(session, deployment, commits) if commits else []
    # A run recorded only because a stage is learning (`anchoring.learning`) is no watched deploy: no incident.
    # Bitbucket names no workflow: its build statuses are recorded by production branch (`bitbucket.py`).
    watched = deployment.forge == SOURCE_BITBUCKET or _watched(project, deployment.workflow_path)
    due = failure_due(deployment, now) and watched
    digest = hashlib.sha256(json.dumps({
        "status": deployment.status, "environment": deployment.environment, "shipped": deployment.shipped,
        "production": deployment.is_production, "anchor": deployment.is_anchor, "range": span.status,
        "from": span.from_sha, "note": span.note, "commits": sorted(set(commits)), "prs": [p[:3] for p in prs],
        "job": deployment.failing_job, "step": deployment.failing_step, "failure_due": due,
    }, sort_keys=True).encode()).hexdigest()
    deployment.range_status, deployment.range_from_sha, deployment.range_note = span.status, span.from_sha, span.note
    if digest == deployment.reconciled_hash:
        session.flush()
        return []
    _replace_children(session, deployment, commits, prs)
    deployment.reconciled_hash = digest
    session.flush()
    edges.on_deployment(session, deployment)
    intents = incidents.record_deploy_failure(session, deployment) if due else []
    if deployment.is_anchor:
        if deployment.status == "success":
            incidents.resolve_deploy_failures(session, deployment)
        linkage.on_deployment(session, deployment)
        regression.schedule(session, deployment)
        probes.schedule(session, deployment)
    anchoring.anchor(session, project, deployment, now)
    session.flush()
    return intents


# ── The cron job ────────────────────────────────────────────────────────────────────────────────────

def _flush(intents: list[RaiseIntent]) -> None:
    from app.production_qa import events

    if intents:
        events.flush(intents)


def _reconcile_and_commit(session: Session, deployment_id: int, now: datetime) -> None:
    """One deployment's reconcile in its own commit, its raises flushed after it (the recorder's doctrine)."""
    deployment = session.scalar(select(Deployment).where(Deployment.id == deployment_id).with_for_update()
                                .execution_options(populate_existing=True))
    if deployment is None:
        return
    intents = reconcile(session, deployment)
    session.commit()
    _flush(intents)


def _recorrelate(session: Session, now: datetime) -> None:
    """Re-run status and job deliveries of the last `RECORRELATE_WINDOW` whose run may have been recorded since."""
    rows = session.execute(
        select(TriggerDelivery, TaskTrigger)
        .join(TaskTrigger, TaskTrigger.id == TriggerDelivery.trigger_id)
        .where(
            TaskTrigger.source_kind == SOURCE_GITHUB,
            TaskTrigger.source_config["record_only"].as_boolean().is_(True),
            TriggerDelivery.received_at >= now - RECORRELATE_WINDOW,
            or_(
                TriggerDelivery.preview["text"].as_string().contains(f'"x-github-event": "{_EVENT_STATUS}"'),
                # Only a failed job is ever recorded: a CI-wide job stream is not re-read.
                TriggerDelivery.preview["text"].as_string().contains(f'"x-github-event": "{_EVENT_JOB}"')
                & or_(*(TriggerDelivery.preview["text"].as_string().contains(f'"conclusion": "{c}"')
                        for c in _FAILED)),
            ),
        )
        .order_by(TriggerDelivery.received_at, TriggerDelivery.id)
        .limit(RECORRELATE_BATCH)
    ).all()
    recorrelate_deliveries(session, rows, _event_of, _recorrelate_one, "production-qa: re-correlating delivery %s failed")


def _recorrelate_one(session: Session, project: Project, event: _Event, row: TriggerDelivery) -> list[Deployment]:
    return (_record_status(session, project, event, row.received_at) if event.name == _EVENT_STATUS
            else _record_job(session, project, event))


def recorrelate_deliveries(session: Session, rows: Iterable[tuple[TriggerDelivery, TaskTrigger]],
                           event_of: Callable[[TaskTrigger, Any], Optional[_Event]],
                           record: Callable[[Session, Project, _Event, TriggerDelivery], Iterable[Deployment]],
                           failure: str) -> None:
    """Re-record each delivery's stored preview (`event_of` reads it; `record` re-records it onto its project) and
    reconcile what it touched, one commit per delivery; a delivery that fails is logged as `failure` (its id) and
    skipped. The GitHub and GitLab sweeps pick their own rows."""
    for row, trigger in rows:
        try:
            stored = json.loads((row.preview or {}).get("text") or "null")
        except ValueError:  # a truncated preview: not one this module wrote
            continue
        event = event_of(trigger, stored)
        project = _project(session, trigger.project_id) if event else None
        if event is None or project is None:
            continue
        try:
            with session.begin_nested():
                intents = reconcile_all(session, record(session, project, event, row))
        except Exception:  # noqa: BLE001 — one delivery's fault must not stop the sweep, nor recur silently
            log.exception(failure, row.id)
            continue
        session.commit()
        _flush(intents)


def deploy_reconcile(session: Session, now: datetime) -> None:
    """The `deploy_reconcile` job: pending ranges, partial recomputes, failure grace, status re-correlation and
    `branch_pushes` retention. Every write is the subject row's own org (`cron.py`)."""
    _recorrelate(session, now)
    clauses = [
        Deployment.range_status == "pending",
        (Deployment.status == "failure") & Deployment.failing_job.is_(None)
        & (func.coalesce(Deployment.finished_at, Deployment.last_received_at) <= now - FAILURE_GRACE)
        & (func.coalesce(Deployment.finished_at, Deployment.last_received_at) > now - FAILURE_GRACE - timedelta(hours=1)),
    ]
    if now.minute % PARTIAL_EVERY_MINUTES == 0:
        clauses.append((Deployment.range_status == "partial")
                       & (func.coalesce(Deployment.finished_at, Deployment.last_received_at)
                          > now - PARTIAL_RECOMPUTE_FOR))
    for deployment_id in list(session.scalars(select(Deployment.id).where(or_(*clauses)).order_by(Deployment.id))):
        _reconcile_and_commit(session, deployment_id, now)
    if now.minute == 0:
        stale = select(BranchPush.id).where(BranchPush.received_at < now - PUSH_RETENTION).limit(_RETENTION_BATCH)
        session.execute(delete(BranchPush).where(BranchPush.id.in_(stale)))


def open_deploy_failures(session: Session, deployment: Deployment) -> list[Incident]:
    """The open `deploy_failed` incidents of this deployment's workflow and environment, or of that workflow with no
    environment known (the stored columns, never a fingerprint prefix: a long key ends in a digest)."""
    return list(session.scalars(
        select(Incident).where(
            Incident.project_id == deployment.project_id, Incident.type == "deploy_failed",
            Incident.state.in_(("open", "reopened")),
            # `unknown`: a run of this workflow that failed before any environment was reported.
            Incident.environment.in_((deployment.environment, "unknown")),
            Incident.facts["workflow_path"].as_string() == deployment.workflow_path,
        ).order_by(Incident.id)
    ))


recorder.register(SOURCE_GITHUB, handle)
cron.register("deploy_reconcile", deploy_reconcile)
