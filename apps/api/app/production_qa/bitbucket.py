"""Bitbucket Cloud deploy ingestion: a record-only `bitbucket` trigger's deliveries mapped onto the rows GitHub's
deploys write (`deployments.py`) — `branch_pushes`, `deployments` (`forge = 'bitbucket'`) and their children — then the
same `deployments.reconcile`, so the range, PRs, `deployed` edges and `deploy_failed` incidents follow one rule.

WHAT BITBUCKET SENDS (support.atlassian.com/bitbucket-cloud/docs/event-payloads/): no deployment event exists. A
pipeline's outcome reaches a webhook as a build status on the commit it ran — `repo:commit_status_created` /
`repo:commit_status_updated`, state `INPROGRESS`, `SUCCESSFUL`, `FAILED` (or `STOPPED`). Pushes are `repo:push`, each
change listing at most 5 commits newest first, `truncated` when there were more. Deliveries are signed
`X-Hub-Signature: sha256=<hex>` with the webhook's secret (`trigger_secrets.verify`, checked at ingress: 401 on a
missing or wrong signature, nothing recorded).

THE MAPPING (Connect deploys › Bitbucket writes `source_config`: `repository`, `production_branch`, `statuses`):
* `repo:push` → one `branch_pushes` row per branch change (oldest commit first, `truncated` as Bitbucket says; a
  created branch's `before` is the zero SHA); a status of a pushed head delivered before its push (no `refname`, so no
  branch yet) is recorded again from its delivery's trimmed copy; then the waiting ranges of the repository reconcile.
* a build status on `production_branch` (its `refname`, else the one branch a recorded push left at that commit — none
  after a fast-forward, where two did) → one `deployments` row per run attempt. The deploy is named by the status
  name without build numbers (`deploy_name`, `workflow_path`), so every run of one deploy folds onto one
  `deploy_failed` incident and its next success resolves it, whatever key a run carries; `statuses` (none listed:
  every one) matches the key, the name or that deploy name. `run_id` is the build number of a bitbucket.org
  Pipelines result URL (`…/results/<n>`), else a stable number from the key and commit; a status sent after its
  attempt concluded is a rerun, a new attempt. `run_event` is `push` (a branch status); `run_url` only an http(s) URL.
  `SUCCESSFUL` ships it to the project's production environment (a `deployment_environments` `success` row: the
  anchor); `FAILED` records it failed there, its job the deploy name and its step the description; `STOPPED` is
  cancelled. An attempt's outcome never changes once recorded.
A status on any other branch, of another repository, or not listed records nothing.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.clock import parse_utc
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.production_qa import BranchPush, Deployment
from app.models.projects import Project
from app.production_qa import deployments, recorder
from app.production_qa.events import RaiseIntent
from app.releases import stages
from app.triggers import BITBUCKET_EVENT_HEADER, SOURCE_BITBUCKET

FORGE = "bitbucket"
_EVENT_PUSH = "repo:push"
_STATUS_EVENTS = ("repo:commit_status_created", "repo:commit_status_updated")
# The environment a production-branch status ships to when the project names none.
DEFAULT_ENVIRONMENT = "production"
_RESULT_IN_URL = re.compile(r"/results/(\d+)")
_COMMIT_IN_HREF = re.compile(r"/commit/([0-9a-fA-F]{40})(?:[/?#]|$)")
_BUILD_NUMBER = re.compile(r"\s*#\d+")
_MAX_TEXT = 500
# The most recent status deliveries one push replays.
_REPLAY_BATCH = 200


def handle(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """The `bitbucket` recorder handler: record the deploy fact, then reconcile what it touched."""
    event = deployments.stored_event(trigger, row.event, source_kind=SOURCE_BITBUCKET, header=BITBUCKET_EVENT_HEADER,
                                     require_named=True)
    if event is None:
        return []
    project = deployments._project(session, trigger.project_id)
    if project is None:
        return []
    received = row.received_at or deployments._now()
    deployments._keep_trimmed(row, BITBUCKET_EVENT_HEADER, event.name, _trimmed(event))
    if event.name == _EVENT_PUSH:
        touched = _record_push(session, project, trigger, event, row.dedup_key, received)
    elif event.name in _STATUS_EVENTS:
        touched = _record_status(session, project, event, received)
    else:
        touched = []
    return deployments.reconcile_all(session, touched)


# ── The stored copy ─────────────────────────────────────────────────────────────────────────────────

def _target(ref: Any) -> Optional[dict[str, Any]]:
    if not isinstance(ref, dict):
        return None
    target = ref.get("target") or {}
    return {"type": ref.get("type"), "name": ref.get("name"),
            "target": {"hash": target.get("hash"), "date": target.get("date")}}


def _trimmed(event: deployments._Event) -> dict[str, Any]:
    """Only the fields this module reads of each event."""
    repo = {"full_name": event.repo_path}
    if event.name == _EVENT_PUSH:
        changes = ((event.body.get("push") or {}).get("changes")) or []
        return {"repository": repo, "push": {"changes": [{
            "new": _target(c.get("new")), "old": _target(c.get("old")), "forced": bool(c.get("forced")),
            "truncated": bool(c.get("truncated")),
            "commits": [{"hash": m.get("hash")} for m in c.get("commits") or [] if isinstance(m, dict)],
        } for c in changes if isinstance(c, dict)]}}
    status = event.body.get("commit_status") or {}
    links = status.get("links") or {}
    return {"repository": repo, "commit_status": {
        **{k: status.get(k) for k in (
            "key", "name", "description", "state", "type", "url", "refname", "created_on", "updated_on")},
        "commit": {"hash": (status.get("commit") or {}).get("hash")},
        "links": {"commit": {"href": (links.get("commit") or {}).get("href")}},
    }}


# ── repo:push ───────────────────────────────────────────────────────────────────────────────────────

def _record_push(session: Session, project: Project, trigger: TaskTrigger, event: deployments._Event,
                 delivery: Optional[str], received: datetime) -> list[Deployment]:
    """Each branch change shaped into the GitHub push `deployments._record_push` records (one row per change)."""
    heads: set[str] = set()
    touched: list[Deployment] = []
    for change in ((event.body.get("push") or {}).get("changes")) or []:
        if not isinstance(change, dict):
            continue
        new, old = change.get("new") or {}, change.get("old") or {}
        target = new.get("target") or {}
        after = deployments._sha(target.get("hash"))
        branch = str(new.get("name") or "").strip()
        if new.get("type") != "branch" or not branch or after is None:
            continue  # a tag, or a deleted branch: no deploy's history
        before = deployments._sha((old.get("target") or {}).get("hash")) if old else deployments._ZERO_SHA
        if before is None:
            continue
        # Bitbucket lists the newest first; the ledger keeps a push's commits oldest first, as GitHub sends them.
        shaped = {"ref": f"refs/heads/{branch}", "before": before, "after": after, "forced": change.get("forced"),
                  "commits": [{"id": c.get("hash")} for c in change.get("commits") or [] if isinstance(c, dict)][::-1],
                  "head_commit": {"id": after, "timestamp": target.get("date")}}
        touched += deployments._record_push(session, project, deployments._Event("push", shaped, event.repo_path),
                                            delivery, received, truncated=bool(change.get("truncated")))
        heads.add(after)
    if not heads:
        return []
    touched += _replay_statuses(session, project, trigger.id, event, heads, received)
    return list({d.id: d for d in touched}.values())


# ── Build statuses ──────────────────────────────────────────────────────────────────────────────────

def _commit_of(status: dict[str, Any]) -> Optional[str]:
    """The status's commit: `commit.hash` where sent, else the SHA ending its documented `links.commit.href`."""
    direct = deployments._sha((status.get("commit") or {}).get("hash"))
    if direct:
        return direct
    match = _COMMIT_IN_HREF.search(str(((status.get("links") or {}).get("commit") or {}).get("href") or ""))
    return match.group(1).lower() if match else None


def _branch_of(session: Session, project: Project, repo_path: str, status: dict[str, Any], sha: str) -> Optional[str]:
    """The status's `refname`, else the one branch a recorded push left at the commit — none when two branches did
    (a fast-forward merge: the feature branch's own build must not read as the production branch's)."""
    refname = str(status.get("refname") or "").strip()
    if refname:
        return refname
    branches = list(session.scalars(
        select(BranchPush.branch).distinct().where(
            BranchPush.project_id == project.id, BranchPush.repo_path == repo_path, BranchPush.after_sha == sha)
        .limit(2)
    ))
    return branches[0] if len(branches) == 1 else None


def run_id_of(key: str, sha: str, url: Any) -> int:
    """The run a status is: the build number of a Bitbucket Pipelines result URL (on bitbucket.org), else a stable
    60-bit number of its key and commit (one status key per commit is one run)."""
    text = str(url or "")
    match = _RESULT_IN_URL.search(text) if urlsplit(text).hostname == "bitbucket.org" else None
    if match and len(match.group(1)) <= 15:
        return int(match.group(1))
    return int(hashlib.sha256(f"{key}\n{sha}".encode()).hexdigest()[:15], 16)


def deploy_name(name: str, key: str) -> str:
    """What stays the same across runs of one deploy: the status name without its build numbers (`Pipeline #12 for
    main` → `Pipeline for main`), else the key. It names the deploy (`workflow_path`) and its failing job, so the next
    failure folds onto the same incident and the next success resolves it, whatever key a run carries."""
    return (_BUILD_NUMBER.sub("", name[:_MAX_TEXT]).strip() or key)[:_MAX_TEXT]


def _web_url(value: Any) -> Optional[str]:
    """A status URL the console may link: http(s) only (a contributor sets it), at most 2048 characters."""
    text = str(value or "").strip()
    return text[:2048] if urlsplit(text).scheme in ("https", "http") else None


def _set_environment(session: Session, deployment: Deployment, environment: str, state: str, sent: datetime) -> None:
    current, created = deployments._environment_row(session, deployment, environment, state, sent)
    if not created and current.state != "success":
        current.state, current.received_at = state, sent


def _attempt(session: Session, project: Project, repo_path: str, run_id: int, workflow: str, sha: str,
             sent: datetime, received: datetime) -> Optional[Deployment]:
    """The run attempt this status reports, locked: the latest one, or a new one when the latest already concluded
    before `sent` (a rerun — Pipelines keeps the build number, another tool the key). None for a row of another
    deploy or forge that shares the run id."""
    latest = session.scalar(
        select(Deployment.run_attempt).where(Deployment.project_id == project.id, Deployment.repo_path == repo_path,
                                             Deployment.run_id == run_id)
        .order_by(Deployment.run_attempt.desc()).limit(1))
    attempt = latest or 1
    if latest is not None:
        current = deployments._locked_run(session, project.id, repo_path, run_id, latest)
        if current is None or current.forge != FORGE or current.workflow_path != workflow or current.head_sha != sha:
            return None
        if current.conclusion_raw is None or current.finished_at is None or sent <= current.finished_at:
            return current
        attempt = latest + 1
    session.execute(
        pg_insert(Deployment)
        .values(
            org_id=project.org_id, project_id=project.id, repo_path=repo_path, forge=FORGE, workflow_path=workflow,
            run_id=run_id, run_attempt=attempt, head_sha=sha, status="queued", first_received_at=received,
            last_received_at=received,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "repo_path", "run_id", "run_attempt"])
    )
    found = deployments._locked_run(session, project.id, repo_path, run_id, attempt)
    return found if found is not None and found.forge == FORGE else None


def _record_status(session: Session, project: Project, event: deployments._Event,
                   received: datetime) -> list[Deployment]:
    status = event.body.get("commit_status") or {}
    key = str(status.get("key") or "").strip()[:_MAX_TEXT]
    name = str(status.get("name") or "").strip()[:_MAX_TEXT]
    state = str(status.get("state") or "").strip().upper()
    sha = _commit_of(status)
    if not key or not state or sha is None or (status.get("type") or "build") != "build":
        return []
    workflow = deploy_name(name, key)
    listed = event.config.get("statuses") or []
    if listed and not {key, name, workflow} & set(listed):
        return []
    production_branch = str(event.config.get("production_branch") or "").strip()
    branch = _branch_of(session, project, event.repo_path, status, sha)
    if not production_branch or branch != production_branch:
        return []
    sent = parse_utc(status.get("updated_on")) or parse_utc(status.get("created_on")) or received
    deployment = _attempt(session, project, event.repo_path, run_id_of(key, sha, status.get("url")), workflow, sha,
                          sent, received)
    if deployment is None:
        return []
    deployment.workflow_name = name or deployment.workflow_name
    deployment.run_event = "push"
    deployment.run_url = _web_url(status.get("url")) or deployment.run_url
    deployment.head_branch = branch
    deployment.started_at = deployment.started_at or sent
    deployment.last_received_at = max(deployment.last_received_at, received)
    if deployment.conclusion_raw is not None:
        # This attempt's outcome stands: a status sent before it (or the same one again) changes nothing.
        session.flush()
        return [deployment]
    environment = stages.production_environment(project, session) or DEFAULT_ENVIRONMENT
    if state == "INPROGRESS":
        deployment.status = "in_progress"
        _set_environment(session, deployment, environment, "in_progress", sent)
    elif state == "SUCCESSFUL":
        deployment.conclusion_raw, deployment.finished_at = "success", sent
        _set_environment(session, deployment, environment, "success", sent)
    elif state == "FAILED":
        deployment.conclusion_raw, deployment.finished_at = "failure", sent
        deployment.failing_job = workflow
        deployment.failing_step = str(status.get("description") or "").strip()[:_MAX_TEXT] or None
        _set_environment(session, deployment, environment, "failure", sent)
    elif state == "STOPPED":
        deployment.conclusion_raw, deployment.finished_at = "cancelled", sent
    session.flush()
    return [deployment]


def _replay_statuses(session: Session, project: Project, trigger_id: Any, push: deployments._Event, shas: set[str],
                     received: datetime) -> list[Deployment]:
    """Statuses of `shas` delivered in the last `deployments.RECORRELATE_WINDOW` before the push that names their
    branch (Pipelines reports a build as the push lands, and the two webhooks race), recorded again from their trimmed
    copies now that the branch is known. Idempotent: a status already recorded changes nothing."""
    touched: list[Deployment] = []
    rows = session.scalars(
        select(TriggerDelivery).where(
            TriggerDelivery.trigger_id == trigger_id,
            TriggerDelivery.received_at >= received - deployments.RECORRELATE_WINDOW,
            TriggerDelivery.preview["text"].as_string().contains('"commit_status"'),
        ).order_by(TriggerDelivery.received_at, TriggerDelivery.id).limit(_REPLAY_BATCH)
    ).all()
    for row in rows:
        # Intake drops a settled delivery's event body; the trimmed copy lives on in its preview.
        stored = row.event or _stored(row) or {}
        body = stored.get("body") or {}
        status = body.get("commit_status") or {}
        if not isinstance(status, dict) or _commit_of(status) not in shas or status.get("refname"):
            continue
        name = str((stored.get("headers") or {}).get(BITBUCKET_EVENT_HEADER) or "")
        event = deployments._Event(name=name, body=body, repo_path=push.repo_path, config=push.config)
        touched.extend(_record_status(session, project, event, row.received_at or received))
    return touched


def _stored(row: TriggerDelivery) -> Optional[dict[str, Any]]:
    import json

    try:
        return json.loads((row.preview or {}).get("text") or "null")
    except ValueError:  # a truncated preview: not one this module wrote
        return None


recorder.register(SOURCE_BITBUCKET, handle)
