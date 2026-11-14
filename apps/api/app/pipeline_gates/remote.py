"""The two gates the control plane judges for the node driver (architecture §3.5): `pipeline.alerts` and
`pipeline.ticket`, each `{run_id, position}`. Everything else (project, environment, severity, credential, query,
the ticket typed when pressed) is read off the stored run, never the body; only the task holding the run's work may
ask (`results.held_run`), anything else is the same 404 as a run that does not exist.

Open alerts (§3.1): Jaah's own incident ledger, read only (N7). States counted: open and reopened. The gate's
`environment`, else the stage's `deploy_environment`, else any; an incident with no environment always counts, since
it cannot be ruled out. At or above `min_severity` when one is set.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.config import console_url
from app.db_async import wait_off_loop
from app.models.production_qa import SEVERITIES, Incident
from app.models.projects import Project
from app.models.releases import GateRun, Release, ReleaseStage
from app.models.tasks import Task
from app.pipeline_gates import results, tickets

#: Most open incidents an alerts answer lists (AC6.1).
ALERTS_LISTED = 5
OPEN_STATES = ("open", "reopened")


def _gate(session: Session, task: Task, body: Mapping[str, Any], kind: str) -> tuple[GateRun, dict[str, Any]]:
    run = results.held_run(session, task, body.get("run_id"))
    try:
        position = int(body.get("position"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("position must be a gate's index in the run") from None
    gates = run.gates or []
    if not 0 <= position < len(gates) or gates[position].get("kind") != kind:
        raise LookupError(f"no {kind} gate at this position")
    return run, dict(gates[position])


def _environment(session: Session, run: GateRun, config: Mapping[str, Any]) -> Optional[str]:
    named = str(config.get("environment") or "").strip()
    if named:
        return named
    stage = session.get(ReleaseStage, run.stage_id) if run.stage_id is not None else None
    return (stage.deploy_environment or "").strip() or None if stage is not None else None


def _incident_url(incident: Incident) -> Optional[str]:
    own = console_url(f"/incidents/{incident.id}")
    if own.startswith("https://"):
        return own
    vendor = str(incident.vendor_url or "")
    return vendor if vendor.startswith("https://") else None


def open_alerts(session: Session, project_id: int, environment: Optional[str],
                min_severity: Optional[str]) -> list[dict[str, Any]]:
    """The project's open incidents the gate counts, worst and newest first, at most `ALERTS_LISTED`."""
    stmt = select(Incident).where(Incident.project_id == project_id, Incident.state.in_(OPEN_STATES))
    if environment:
        stmt = stmt.where(or_(Incident.environment.is_(None), Incident.environment == environment))
    if min_severity in SEVERITIES:
        stmt = stmt.where(Incident.severity.in_(SEVERITIES[SEVERITIES.index(min_severity):]))
    rank = case({s: i for i, s in enumerate(SEVERITIES)}, value=Incident.severity, else_=-1)
    rows = session.scalars(stmt.order_by(rank.desc(), Incident.last_seen.desc(), Incident.id.desc())
                           .limit(ALERTS_LISTED))
    return [{"title": r.title, "severity": r.severity, "url": _incident_url(r)} for r in rows]


def alerts(session: Session, task: Task, body: Mapping[str, Any]) -> dict[str, Any]:
    """`pipeline.alerts` → `{open: [{title, severity, url}], source_connected}`."""
    from app.releases.pipeline import has_alert_source  # noqa: PLC0415 — the releases package imports this one

    run, gate = _gate(session, task, body, "alerts")
    config = gate.get("config") or {}
    project = session.get(Project, run.project_id)
    return {
        "open": open_alerts(session, run.project_id, _environment(session, run, config), config.get("min_severity")),
        "source_connected": has_alert_source(session, project),
    }


def _asked_ticket(session: Session, run: GateRun) -> Optional[str]:
    if run.subject_kind != "release" or not str(run.subject_id).isdigit():
        return None
    release = session.get(Release, int(run.subject_id))
    value = (release.inputs or {}).get("ticket") if release is not None else None
    return str(value).strip() or None if value else None


def ticket(session: Session, task: Task, body: Mapping[str, Any]) -> dict[str, Any]:
    """`pipeline.ticket` → `{passed, report, read, link?}` (`read`: the ticket was read and is in another state). The credential resolves under the task's launcher, the
    broker's own rule for what this run may reach."""
    from app.routes.agent.task_secrets import _task_grant_principal  # noqa: PLC0415 — the broker's launcher rule

    run, gate = _gate(session, task, body, "ticket")
    config = gate.get("config") or {}
    system = str(config.get("system") or "")
    try:
        credential = tickets.resolve(session, _task_grant_principal(session, task), run.project_id,
                                     gate.get("credential"), system)
        conn = tickets.connect(session, credential, system)
    except tickets.TicketError as exc:
        return {"passed": False, "report": str(exc)}
    asked = _asked_ticket(session, run)
    # The vendor calls never run on the serving loop (the toolbox body is a greenlet on it).
    verdict = wait_off_loop(session, tickets.judge, conn, config, asked)
    out: dict[str, Any] = {"passed": verdict.passed, "report": verdict.report}
    if verdict.read:
        out["read"] = True
    if verdict.link and verdict.link.startswith("https://"):
        out["link"] = verdict.link
    return out
