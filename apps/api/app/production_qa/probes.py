"""Native post-deploy probes (architecture §10, A-8): a project's health URLs and smoke commands, run from a node
serving the project right after its production deploy succeeds; a failure is a `health_breach` incident.

THE PROBES (`projects.health_probes`, `projects.smoke_commands`; written through `PUT /api/projects/{id}`, so only a
`projects:edit` holder authors them — the role that already sets the gate commands a runner executes, so no new trust
boundary). `validated_health_probes` / `validated_smoke_commands` are the only shape rules: https URLs with a host and
no credentials, at most `MAX_HEALTH_PROBES`, an `expect_status`; at most `MAX_SMOKE_COMMANDS` commands, each with a
`timeout_s` of at most `MAX_TIMEOUT_S`. Every probe carries an `id` (kept across edits, assigned when absent) that
keys its incident: `fingerprints.probe(environment, id)`.

THE RUN. `schedule` (from `deployments.reconcile`, on every recomputed anchor) files ONE hidden `deploy_probe` task per
deployment (`dedup_key = probe:{deployment_id}`, any state, ever) when the deployment is an anchor whose run
concluded `success`, the project declares at least one probe, `gate.state` is enabled and the opt-in has its stamp
(`production_qa_enabled_by`, who the run is started as). Gate OFF → nothing is filed. The task dispatches like any
runner task to a CP-leased slot on a node serving the project; its cheapest-tier agent runs one command,
`tools/deploy_probe.py run`, which asks `plan` for the probes and reports `ingest`'s results (verbs
`production_qa.probe_plan` / `production_qa.probe_result`, `routes/agent_deploy_probe.py`). The model judges nothing.

THE SANDBOX (A7). A smoke command runs in the slot under the merge lane's fence (`tools/merge_lane.py::sandbox_argv`:
`bwrap`, daemon socket hidden, git dirs read-only, secret-shaped environment dropped) — what a runner session on
that node may do, nothing more; no `bwrap`, no run. HTTP probes are one GET each, `HTTP_TIMEOUT_S`, no cross-host or
downgrading redirect. Output tails are capped at `OUTPUT_TAIL_BYTES` and redacted in the slot and again here.

THE RESULTS. `plan` snapshots what it hands out on the task (`metrics.probes`); `ingest` accepts one report per run,
only for those probes: a failure records a `health_breach` occurrence on its fingerprint (`deployment_id` set: the
Deployments "Probes" tab), a pass resolves it; the transitions raise onto the `health_breach` automations
(`events.flush`, after the caller's commit) like any incident. A gate turned off between filing and run makes
`plan` answer `skipped`: nothing runs; so does a later production deploy of the same environment (`superseded`), and
a run reporting after one keeps its results but moves no incident. A probe removed from the settings has its open
incident resolved by the next run. Smoke commands run in the slot's own checkout (the integration branch the slot
was leased on), not a checkout of the deployed commit.

TRUST. The results are the slot runner's word, as the outcome scan's facts are: the agent could call the result
verb itself, which its charter forbids; the reach stays inside its own run's planned probes.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import git_access, secret_redact
from app.clock import utcnow
from app.events import emit_console_event
from app.models.production_qa import Deployment
from app.models.projects import Project
from app.models.tasks import Task
from app.production_qa import fingerprints, gate
from app.production_qa.events import RaiseIntent

log = logging.getLogger("cockpit-production-qa")

PROBE_PERSONA = "deploy_probe"
MAX_HEALTH_PROBES = 10
MAX_SMOKE_COMMANDS = 5
MAX_TIMEOUT_S = 300
DEFAULT_TIMEOUT_S = 60
DEFAULT_EXPECT_STATUS = 200
HTTP_TIMEOUT_S = 10
OUTPUT_TAIL_BYTES = 4096
_NAME_MAX = 100
_URL_MAX = 2000
_COMMAND_MAX = 2000
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
#: `tasks.metrics` keys of a probe run.
DEPLOYMENT_KEY, PLAN_KEY, REPORTED_KEY, RESULTS_KEY = "deployment_id", "probes", "reported_at", "probe_results"
SKIPPED_KEY = "skipped"
#: `plan`'s skip reason when a later production deploy exists (beside `gate.GateReason`'s two).
SUPERSEDED = "superseded"
#: The most result items a report may carry (a body longer than any plan is refused, not read).
_MAX_RESULT_ITEMS = 100


class ResultConflict(Exception):
    """The run already reported (one report per run)."""


def dedup_key(deployment_id: int) -> str:
    return f"probe:{deployment_id}"


# ── The settings ─────────────────────────────────────────────────────────────────────────────────────


def _rows(value: Any, what: str, cap: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise ValueError(f"{what} is a list of objects.")
    if len(value) > cap:
        raise ValueError(f"List at most {cap} {what}.")
    return value


def _name(row: dict[str, Any], fallback: str) -> str:
    name = str(row.get("name") or "").strip() or fallback
    if len(name) > _NAME_MAX:
        raise ValueError(f"A probe name is at most {_NAME_MAX} characters.")
    return name


def _id(row: dict[str, Any], seen: set[str]) -> str:
    probe_id = row.get("id")
    if probe_id in (None, ""):
        probe_id = secrets.token_hex(4)
    if not isinstance(probe_id, str) or not _ID.fullmatch(probe_id):
        raise ValueError("A probe id is 1-40 lowercase letters, digits, '-' or '_'.")
    if probe_id in seen:
        raise ValueError(f"Probe id {probe_id} is used twice.")
    seen.add(probe_id)
    return probe_id


def _int(value: Any, default: int) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def validated_health_probes(value: Any, seen: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """The health URLs as stored: `[{id, name, url, expect_status}]`. ValueError names the first problem."""
    seen = set() if seen is None else seen
    out = []
    for row in _rows(value, "health URLs", MAX_HEALTH_PROBES):
        url = str(row.get("url") or "").strip()
        parts = urlsplit(url)
        if len(url) > _URL_MAX or parts.scheme != "https" or not parts.hostname or any(c.isspace() for c in url):
            raise ValueError("A health URL is an https:// address.")
        if parts.username is not None or parts.password is not None:
            raise ValueError("A health URL carries no credentials.")
        status = _int(row.get("expect_status"), DEFAULT_EXPECT_STATUS)
        if status is None or not 100 <= status <= 599:
            raise ValueError("Set the expected status between 100 and 599.")
        out.append({"id": _id(row, seen), "name": _name(row, parts.hostname), "url": url, "expect_status": status})
    return out


def validated_smoke_commands(value: Any, seen: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """The smoke commands as stored: `[{id, name, command, timeout_s}]`. ValueError names the first problem."""
    seen = set() if seen is None else seen
    out = []
    for row in _rows(value, "smoke commands", MAX_SMOKE_COMMANDS):
        command = str(row.get("command") or "").strip()
        if not command or len(command) > _COMMAND_MAX or "\x00" in command:
            raise ValueError(f"A smoke command is 1-{_COMMAND_MAX} characters.")
        timeout = _int(row.get("timeout_s"), DEFAULT_TIMEOUT_S)
        if timeout is None or not 1 <= timeout <= MAX_TIMEOUT_S:
            raise ValueError(f"Set the timeout between 1 and {MAX_TIMEOUT_S} seconds.")
        out.append({"id": _id(row, seen), "name": _name(row, command.split()[0][:_NAME_MAX]), "command": command,
                    "timeout_s": timeout})
    return out


def validated(fields: dict[str, Any], current: Optional[Project]) -> dict[str, Any]:
    """`ProjectUpdate`'s probe fields, normalized; ids unique across both lists (the one not sent keeps its stored
    ids in the count). ValueError names the first problem; a null clears the list."""
    out: dict[str, Any] = {}
    sent = [name for name in ("health_probes", "smoke_commands") if name in fields]
    if not sent:
        return out
    seen: set[str] = set()
    for name in ("health_probes", "smoke_commands"):
        if name not in fields:
            seen.update(str(r.get("id")) for r in (getattr(current, name, None) or []) if isinstance(r, dict))
    if "health_probes" in fields:
        out["health_probes"] = validated_health_probes(fields["health_probes"], seen)
    if "smoke_commands" in fields:
        out["smoke_commands"] = validated_smoke_commands(fields["smoke_commands"], seen)
    return out


# ── The run ──────────────────────────────────────────────────────────────────────────────────────────


def schedule(session: Session, deployment: Deployment) -> None:
    """File the probe run for a successful production anchor when the gate is on (module docstring). Repeat-safe:
    one run per deployment, ever. Flushes, never commits."""
    if not deployment.is_anchor or deployment.status != "success":
        return
    project = session.get(Project, deployment.project_id)
    if project is None or not (project.health_probes or project.smoke_commands):
        return
    if not gate.state(session, project).enabled:
        return
    # A background job: run as the project's "Run jobs as" person, else the org's (`git_access.project_forge_person`).
    person = git_access.project_forge_person(session, project)
    if person is None:
        log.warning("production-qa: project %s has nobody to run its probes as; probes not filed", project.id)
        return
    key = dedup_key(deployment.id)
    if session.scalar(select(Task.id).where(Task.project_id == project.id, Task.persona == PROBE_PERSONA,
                                            Task.dedup_key == key).limit(1)) is not None:
        return
    task = Task(
        project_id=project.id, org_id=project.org_id,
        title=f"Deploy probes: {project.name} {deployment.environment} {deployment.head_sha[:7]}"[:300],
        body=(f"Deploy probes for deployment {deployment.id} ({deployment.environment}, `{deployment.head_sha}`): run "
              "this project's health URLs and smoke commands and report them. Filed by the control plane "
              "(`app/production_qa/probes.py::schedule`)."),
        dedup_key=key, persona=PROBE_PERSONA, state="ready", hidden=True, parent_task_id=None,
        created_by_id=person, started_by_id=person, metrics={DEPLOYMENT_KEY: deployment.id},
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        return  # a concurrent reconcile filed it
    emit_console_event(session, entity="task", id=task.id, action="ready")
    log.info("production-qa: filed deploy probes task %s for deployment %s", task.id, deployment.id)


def _deployment(session: Session, task: Task) -> Deployment:
    deployment_id = (task.metrics or {}).get(DEPLOYMENT_KEY)
    deployment = session.get(Deployment, deployment_id) if isinstance(deployment_id, int) else None
    if deployment is None or deployment.project_id != task.project_id:
        raise LookupError("no deployment for this probe run")
    return deployment


def _superseded(session: Session, deployment: Deployment) -> bool:
    """A later successful production anchor of the same environment exists: production no longer runs this one, so
    its probes would judge (and pin incidents on) code that has moved on."""
    return session.scalar(select(Deployment.id).where(
        Deployment.project_id == deployment.project_id, Deployment.environment == deployment.environment,
        Deployment.is_anchor.is_(True), Deployment.status == "success", Deployment.id != deployment.id,
        Deployment.finished_at > (deployment.finished_at or deployment.last_received_at),
    ).limit(1)) is not None


def plan(session: Session, task: Task) -> dict[str, Any]:
    """What the run probes: the project's probes now, snapshotted on the task (`ingest` accepts only these), or
    `skipped` with the reason — the gate is off, or a later production deploy superseded this one. LookupError: the
    task names no deployment of its project."""
    deployment = _deployment(session, task)
    project = session.get(Project, task.project_id)
    if project is None:
        raise LookupError("no project for this probe run")
    state = gate.state(session, project)
    skipped = state.reason if not state.enabled else SUPERSEDED if _superseded(session, deployment) else None
    health = list(project.health_probes or []) if skipped is None else []
    smoke = list(project.smoke_commands or []) if skipped is None else []
    task.metrics = {**(task.metrics or {}), PLAN_KEY: {"health_probes": health, "smoke_commands": smoke},
                    SKIPPED_KEY: skipped}
    return {
        "deployment_id": deployment.id, "environment": deployment.environment, "skipped": skipped,
        "health_probes": health, "smoke_commands": smoke,
        "http_timeout_s": HTTP_TIMEOUT_S, "output_tail_bytes": OUTPUT_TAIL_BYTES,
    }


def _tail(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = text.encode("utf-8")[-OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")
    text = secret_redact.redacted(text)
    return text.encode("utf-8")[-OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")


def _redacted(value: Any) -> Optional[str]:
    """A probe's URL or command as an incident fact (facts reach task bodies and chat cards): secret-shaped values
    replaced."""
    return secret_redact.redacted(value) if isinstance(value, str) else None


def _resolve_removed(session: Session, deployment: Deployment, planned: set[str], at: datetime) -> None:
    """Resolve the open probe incidents of this environment whose probe is no longer declared (a removed probe never
    runs again, so nothing else would ever resolve it)."""
    from app.models.production_qa import Incident
    from app.production_qa import incidents

    prefix = fingerprints.probe(deployment.environment, "x")[:-1]
    keep = {fingerprints.probe(deployment.environment, probe_id) for probe_id in planned}
    stale = session.scalars(select(Incident).where(
        Incident.project_id == deployment.project_id, Incident.type == "health_breach",
        Incident.source_kind == "probe", Incident.state != "resolved", Incident.fingerprint.startswith(prefix, autoescape=True),
    )).all()
    for incident in stale:
        if incident.fingerprint in keep:
            continue
        incidents.record(session, deployment.project_id, incidents.resolution(
            "health_breach", "probe", incident.fingerprint, title=incident.title, severity=incident.severity,
            deployment=deployment, at=at,
        ))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(float(value), 3)


def _code(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def ingest(session: Session, task: Task, body: Any) -> tuple[dict[str, Any], list[RaiseIntent]]:
    """Record one run's results (module docstring): a failure opens or folds its `health_breach` incident, a pass
    resolves it. ValueError for a body no report could mean; ResultConflict for a second report; LookupError for a
    task without its deployment or plan. A result for a probe the plan did not hand out is dropped. Flushes, never
    commits; the caller flushes the returned intents after its commit."""
    from app.production_qa import incidents

    deployment = _deployment(session, task)
    session.refresh(task, with_for_update=True)  # one report per run, even for two concurrent calls
    metrics = dict(task.metrics or {})
    if metrics.get(REPORTED_KEY):
        raise ResultConflict("this probe run already reported")
    planned = metrics.get(PLAN_KEY)
    if not isinstance(planned, dict):
        raise LookupError("this probe run has no plan; call production_qa.probe_plan first")
    if metrics.get(SKIPPED_KEY):
        raise ResultConflict(f"this probe run was skipped ({metrics[SKIPPED_KEY]}); there is nothing to report")
    if not isinstance(body, dict) or body.get("deployment_id") != deployment.id or not isinstance(
            body.get("results"), list) or len(body["results"]) > _MAX_RESULT_ITEMS:
        raise ValueError("a report is {deployment_id, results: [...]} for this run's deployment")
    # A run that reports after a later deploy shipped keeps its results on its own row but moves no incident: the
    # stale run neither reopens what the newer run resolved nor pins a failure on code production no longer runs.
    live = not _superseded(session, deployment)
    declared: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, name in (("http", "health_probes"), ("command", "smoke_commands")):
        for row in planned.get(name) or []:
            declared[str(row.get("id"))] = (kind, row)
    now = utcnow()
    results: list[dict[str, Any]] = []
    intents: list[RaiseIntent] = []
    for item in body["results"]:
        if len(results) == len(declared):
            break
        if not isinstance(item, dict) or not isinstance(item.get("ok"), bool):
            continue
        probe_id = str(item.get("probe_id"))
        if probe_id not in declared or declared[probe_id][0] != item.get("kind") or any(
                r["probe_id"] == probe_id for r in results):
            continue
        kind, row = declared[probe_id]
        result = {
            "probe_id": probe_id, "kind": kind, "name": row.get("name") or probe_id, "ok": item["ok"],
            "status": _code(item.get("status")) if kind == "http" else None,
            "exit_code": _code(item.get("exit_code")) if kind == "command" else None,
            "duration_s": _number(item.get("duration_s")), "output_tail": _tail(item.get("output_tail")),
            "incident_id": None,
        }
        facts = {**{k: result[k] for k in ("kind", "name", "status", "exit_code", "duration_s", "output_tail")},
                 "probe_id": probe_id, "task_id": task.id,
                 **({"url": _redacted(row.get("url")), "expect_status": row.get("expect_status")} if kind == "http"
                    else {"command": _redacted(row.get("command")), "timeout_s": row.get("timeout_s")})}
        results.append(result)
        if not live:
            continue
        recorded, raised = incidents.record(session, deployment.project_id, incidents.Observation(
            type="health_breach", source_kind="probe", trigger_id=None,
            fingerprint=fingerprints.probe(deployment.environment, probe_id),
            title=f"Probe failed: {result['name']}", severity="error", environment=deployment.environment,
            occurred_at=now, count=None, count_mode="increment", action="resolve" if result["ok"] else "occurrence",
            release=None, commit_sha=deployment.head_sha, stack_frames=None, vendor_url=None, vendor_ref=None,
            facts=facts, deployment_id=deployment.id, delivery_id=None,
        ))
        if recorded.incident is not None and not result["ok"]:
            result["incident_id"] = recorded.incident.id
        intents.extend(raised)
    if live:
        _resolve_removed(session, deployment, set(declared), now)
    task.metrics = {**metrics, REPORTED_KEY: now.isoformat(), RESULTS_KEY: results,
                    **({} if live else {SKIPPED_KEY: SUPERSEDED})}
    session.flush()
    failed = sum(1 for r in results if not r["ok"])
    return {"recorded": len(results), "failed": failed,
            "incident_ids": [r["incident_id"] for r in results if r["incident_id"]]}, intents


def run_of(session: Session, deployment_id: int, project_id: int) -> Optional[Task]:
    """The deployment's probe run, if one was filed (the Deployments detail reads its results)."""
    return session.scalars(
        select(Task).where(Task.project_id == project_id, Task.persona == PROBE_PERSONA,
                           Task.dedup_key == dedup_key(deployment_id)).order_by(Task.id.desc()).limit(1)
    ).first()


