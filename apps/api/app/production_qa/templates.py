"""The event a CP-raised incident delivery carries (`events.flush`), and the default Task body that renders it.

The event's namespaces are the trigger template's `incident`, `suspects` and `deployment` (`trigger_template.py`):
`incident` the incident with its `transition` and its `brief` (the whole of it as markdown, `brief`), `suspects` the
suggested and confirmed suspect PRs, strongest first, `deployment` the deployment it names (a failed deploy's run) or
`{}`. The template language has no loops, so the brief is rendered here, once, and `DEFAULT_BODY` is little more than
`{{incident.brief}}`. The brief always ends `Incident: INC-<id> <url>`: the line a fix PR quotes so the incident and
its fix link up (`linkage.py`, the fix loop).

A failed deploy's task carries the run, the failing job and step, the commit range and its PRs; the job's log tail is
the runner's to fetch (`jaah-forge ci log`, operator answer R2), never read here.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utc_iso_or_none
from app.models.production_qa import Deployment, DeploymentPr, Incident, IncidentSuspect

# The frames, suspects and PRs the event and brief carry at most.
TOP_FRAMES = 5
MAX_SUSPECTS = 10
MAX_PRS = 30
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

# The Task body a default automation files (`events.DEFAULTS`), and the starting point the editor offers.
DEFAULT_BODY = (
    "Triage this production incident: find the cause, then fix it through a normal PR — or settle with why no code "
    "change is needed. A failed deploy's log tail: `jaah-forge ci log`. Quote the last line in the PR body.\n\n"
    "{{incident.brief}}"
)
# A fixed prefix, so the monitor-sent title never opens the Task's title line on its own.
DEFAULT_TITLE = "{{incident.ref}}: {{incident.title}}"


def _frontend() -> str:
    from app.config import console_url

    return console_url()


def incident_url(incident_id: int) -> str:
    return f"{_frontend()}/incidents/{incident_id}"


def incident_ref(incident_id: int) -> str:
    return f"INC-{incident_id}"


# The PR page of each forge deploys are ingested from (`deployments.py`, `bitbucket.py`).
_PR_PAGES = {"github": "https://github.com/{repo}/pull/{n}", "bitbucket": "https://bitbucket.org/{repo}/pull-requests/{n}"}


def _pr_url(repo_path: str, pr_number: Optional[int], forge: Optional[str] = "github") -> Optional[str]:
    """A PR link for an `owner/repo` path on `forge` (GitHub when unknown)."""
    if pr_number is None or repo_path.count("/") != 1:
        return None
    return _PR_PAGES.get(forge or "github", _PR_PAGES["github"]).format(repo=repo_path, n=pr_number)


def _suspects(session: Session, incident_id: int, forge: Optional[str]) -> list[dict[str, Any]]:
    rows = session.scalars(select(IncidentSuspect).where(
        IncidentSuspect.incident_id == incident_id, IncidentSuspect.state != "dismissed",
    )).all()
    rows = sorted(rows, key=lambda r: (r.state != "confirmed", _CONFIDENCE_ORDER.get(r.confidence, 9), -r.score, r.id))
    return [
        {"pr": row.pr_number, "pr_url": _pr_url(row.repo_path, row.pr_number, forge), "repo_path": row.repo_path,
         "task_id": row.task_id, "confidence": row.confidence, "basis": row.basis, "confirmed": row.state == "confirmed"}
        for row in rows[:MAX_SUSPECTS]
    ]


def _deployment(session: Session, incident: Incident) -> dict[str, Any]:
    """The deployment the incident names, from its row; a failed deploy's facts fill what the row lacks."""
    facts = incident.facts or {}
    row = (session.scalar(select(Deployment).where(Deployment.id == incident.deployment_id))
           if incident.deployment_id is not None else None)
    if row is None and not facts.get("run_url"):
        return {}
    prs = facts.get("prs") or []
    if row is not None:
        prs = sorted(session.scalars(select(DeploymentPr.pr_number).where(DeploymentPr.deployment_id == row.id)))
        span = (f"{row.range_from_sha[:7]}..{row.head_sha[:7]}" if row.range_from_sha else row.head_sha[:7])
    else:
        span = facts.get("range")
    repo = row.repo_path if row is not None else ""
    return {
        "id": row.id if row is not None else None,
        "url": f"{_frontend()}/deployments/{row.id}" if row is not None else None,
        "environment": row.environment if row is not None else incident.environment,
        "workflow": (row.workflow_name or row.workflow_path) if row is not None else facts.get("workflow_path"),
        "run_url": (row.run_url if row is not None else None) or facts.get("run_url"),
        "head_sha": row.head_sha if row is not None else incident.commit_sha,
        "range": span,
        "range_status": row.range_status if row is not None else None,
        "failing_job": (row.failing_job if row is not None else None) or facts.get("failing_job"),
        "failing_step": (row.failing_step if row is not None else None) or facts.get("failing_step"),
        "prs": [{"pr": n, "pr_url": _pr_url(repo, n, row.forge if row is not None else None)}
                for n in list(prs)[:MAX_PRS]],
    }


def _frame(frame: dict[str, Any]) -> str:
    where = str(frame.get("path") or "?")
    if frame.get("lineno") is not None:
        where += f":{frame['lineno']}"
    function = frame.get("function")
    return f"`{where}`" + (f" in `{function}`" if function else "")


def _link(text: str, url: Optional[str]) -> str:
    return f"[{text}]({url})" if url else text


def _regression(facts: dict[str, Any]) -> list[str]:
    """A regression's measurement: the metric, both values, the delta (absolute and %) and the windows."""
    from app.production_qa.regression import METRIC_ERROR_RATE, format_value

    metric = facts["metric"]
    delta = facts.get("delta_abs")
    if delta is None:
        change = "—"
    elif metric == METRIC_ERROR_RATE:
        change = f"{delta * 100:+.2f} pts"
    else:
        change = f"{delta:+.0f} ms"
    if facts.get("delta_pct") is not None:
        change += f" ({facts['delta_pct']:+.0f}%)"
    window = facts.get("window") or {}
    lines = ["Regression:", f"- Metric: {metric.replace('_', ' ')}",
             f"- Baseline: {format_value(metric, facts.get('baseline'))}",
             f"- After deploy: {format_value(metric, facts.get('post'))}", f"- Delta: {change}"]
    if facts.get("events_post") is not None:
        lines.append(f"- Errors after deploy: {facts['events_post']}")
    if window.get("post") and window.get("baseline"):
        lines.append(f"- Window: {window.get('minutes')} min after the deploy ({window['post'][0]} to "
                     f"{window['post'][1]}), against the same length before it ({window['baseline'][0]} to "
                     f"{window['baseline'][1]})")
    return lines


def brief(incident: dict[str, Any], suspects: list[dict[str, Any]], deployment: dict[str, Any]) -> str:
    """The whole incident as markdown, ending with the `Incident: INC-<id> <url>` line."""
    lines = [f"**{incident['title']}**", ""]
    env = incident.get("environment") or "no environment"
    lines.append(f"- {incident['type'].replace('_', ' ').capitalize()} · {incident['severity']} · {env} · "
                 f"{incident['state']} ({incident['transition']})")
    lines.append(f"- Fingerprint: `{incident['fingerprint']}`")
    lines.append(f"- Seen {incident['count']} time{'s' if incident['count'] != 1 else ''}: first "
                 f"{incident['first_seen']}, last {incident['last_seen']}")
    if incident.get("release") or incident.get("commit_sha"):
        parts = [f"release `{incident['release']}`" if incident.get("release") else "",
                 f"commit `{incident['commit_sha']}`" if incident.get("commit_sha") else ""]
        lines.append("- " + ", ".join(p for p in parts if p).capitalize())
    if incident.get("vendor_url"):
        lines.append(f"- Monitor: {incident['vendor_url']}")
    if incident.get("top_frames"):
        lines += ["", "Top stack frames:"] + [f"- {_frame(f)}" for f in incident["top_frames"]]
    lane = (incident.get("facts") or {}) if incident.get("type") == "lane_slowdown" else {}
    if lane.get("stage"):
        prs = lane.get("prs_since_shift") or []
        lines += ["", "Merge-lane stage:",
                  f"- Stage: `{lane['stage']}`",
                  f"- Recent median: {lane.get('recent_median_seconds')}s (last {lane.get('recent_runs')} passed runs)",
                  f"- Baseline median: {lane.get('baseline_median_seconds')}s (the {lane.get('baseline_runs')} before)",
                  "- PRs since the shift: " + (", ".join(f"#{n}" for n in prs) if prs else "none known")]
    if incident["type"] == "regression" and (incident.get("facts") or {}).get("metric"):
        lines += ["", *_regression(incident["facts"])]
    lines += ["", "Suspects:"]
    if suspects:
        for s in suspects:
            pr = _link(f"PR #{s['pr']}", s["pr_url"]) if s["pr"] is not None else "a commit without a known PR"
            task = f", Task #{s['task_id']}" if s.get("task_id") else ""
            mark = ", confirmed" if s.get("confirmed") else ""
            lines.append(f"- {pr} ({s['repo_path']}{task}) — {s['confidence']} confidence, basis {s['basis']}{mark}")
    else:
        lines.append("- No suspect")
    if deployment:
        lines += ["", "Deployment:"]
        if deployment.get("run_url"):
            lines.append(f"- Run: {deployment['run_url']}")
        if deployment.get("workflow"):
            lines.append(f"- Workflow: {deployment['workflow']} ({deployment.get('environment') or 'unknown'})")
        if deployment.get("failing_job"):
            step = f", step `{deployment['failing_step']}`" if deployment.get("failing_step") else ""
            lines.append(f"- Failing job: `{deployment['failing_job']}`{step}")
        if deployment.get("range"):
            lines.append(f"- Commits: `{deployment['range']}`")
        prs = deployment.get("prs") or []
        lines.append("- PRs: " + (", ".join(_link(f"#{p['pr']}", p["pr_url"]) for p in prs) if prs else "none known"))
        if deployment.get("url"):
            lines.append(f"- Deployment: {deployment['url']}")
    lines += ["", f"Incident: {incident_ref(incident['id'])} {incident['url']}"]
    return "\n".join(lines)


def event(session: Session, incident: Incident, transition: str) -> dict[str, Any]:
    """The event of one incident transition, as `events.flush` writes it onto each matching delivery."""
    out: dict[str, Any] = {
        "id": incident.id, "ref": incident_ref(incident.id), "url": incident_url(incident.id), "type": incident.type,
        "title": incident.title, "fingerprint": incident.fingerprint, "severity": incident.severity,
        "environment": incident.environment, "state": incident.state, "count": incident.count,
        "first_seen": utc_iso_or_none(incident.first_seen), "last_seen": utc_iso_or_none(incident.last_seen), "release": incident.release,
        "commit_sha": incident.commit_sha, "vendor_url": incident.vendor_url,
        "top_frames": list((incident.stack_frames or [])[:TOP_FRAMES]), "facts": dict(incident.facts or {}),
        "transition": transition,
    }
    from app.models.projects import Project

    # The PR links' forge: the incident's deploy's, else its project's.
    forge = (session.scalar(select(Deployment.forge).where(Deployment.id == incident.deployment_id))
             if incident.deployment_id else None) or session.scalar(
        select(Project.forge).where(Project.id == incident.project_id))
    suspects = _suspects(session, incident.id, forge)
    deployment = _deployment(session, incident)
    out["brief"] = brief(out, suspects, deployment)
    return {"incident": out, "suspects": suspects, "deployment": deployment}
