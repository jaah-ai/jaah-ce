"""Incident → task (architecture §6): what an incident transition raises, the after-commit raise onto the CP-raised
trigger kinds, the checks those deliveries meet, the dedup claim, and the quiet-hours hold.

THE PATH OF ONE RAISE
1. `incidents.record` returns a `RaiseIntent` for a transition that may file (opened, reopened, a throttled
   occurrence). The recorder's processor (or a cron job) hands it to `flush` AFTER its own commit.
2. `flush` (own session, never raises) writes one delivery per trigger of the intent's kind on the incident's project
   — `received`, or `skipped` for a paused or failed trigger (the `user_registered` rule) — with
   `dedup_key = incident:{id}:{transition}:{incident_event_id}`, and wakes the processor.
3. The processor (`trigger_intake._Processor._handle`) asks `admission` for these kinds only, in order: the gate
   (`skipped`: `production_qa_off` / `project_not_opted_in`); type, environment, severity (`filtered`); quiet hours
   (`held` until the window ends, `not_before`, never a draft — A-1); the incident resolved meanwhile (`filtered`
   `incident_resolved`). A held row is flipped back to `received` by the `release_held` cron job and meets every
   check again.
4. The hourly cap is the trigger's own. The fire engine (`automation_fire._fire_event`) calls `pre_file` before it
   files: the incident's advisory lock, then the claim (`incident_tasks`, one unreleased non-revert claim per
   incident). An open task — ANY state but `done`, a draft included (A-13) — or a filing in flight makes the delivery
   `deduped` `incident_task_open`; a claim whose task is done or gone is released first. `post_file` binds the claim to
   the filed task (role `fix` for a build persona, `triage` for any other). A firing that files nothing releases it
   (`release_claim`).
5. For a `regression` incident, `post_file` also files the revert the project's `revert_policy` asks for
   (`_file_revert`): one coder task, a draft or started, claimed `role='revert'`; it merges only through the merge lane
   like any PR. No incident automation, no revert (`revert_note` says so on the incident).

Incident-family automations file into the incident's own project: their Target must be exactly that one project
(`automation_service`, refused at save), so a trigger's `project_id` is the project whose incidents it hears.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.tasks import Task

log = logging.getLogger("cockpit-production-qa")

RaiseSource = Literal["incident", "deploy_failed", "health_breach"]

# The advisory-lock keys: a namespaced text hashed to one bigint (`hashtextextended`), so a bigint id fits and no other
# user of advisory locks collides.
def lock_key(kind: str, value: int) -> Any:
    return func.hashtextextended(f"production_qa:{kind}:{value}", 0)


# `pre_file`'s refusal when the incident is not the target project's (a trigger edited away from its automation).
REASON_OTHER_PROJECT = "the incident is another project's"


@dataclass(frozen=True)
class RaiseIntent:
    incident_id: int
    source_kind: RaiseSource
    transition: str


@dataclass(frozen=True)
class Verdict:
    """Why a delivery stops before filing: its final (or `held`) state, the reason code, and a hold's release time."""

    state: str
    reason: str
    not_before: Optional[datetime] = None


# ── 2. The raise ─────────────────────────────────────────────────────────────────────────────────────


def flush(intents: list[RaiseIntent]) -> None:
    """Write one delivery per matching trigger for each intent, in its own session, after the recording commit;
    never raises (a lost raise is logged, the recording stands)."""
    for intent in intents:
        try:
            _raise(intent)
        except Exception:  # noqa: BLE001 — one intent's fault never drops the others
            log.exception("production-qa: raising incident %s (%s) failed", intent.incident_id, intent.transition)


def _raise(intent: RaiseIntent) -> None:
    from app.db import BridgedSession
    from app.models.production_qa import Incident, IncidentEvent
    from app.production_qa import templates
    from app.tenancy import seam
    from app.trigger_intake import fleet_session, ingest_incident

    with fleet_session() as session:
        found = session.execute(
            select(Incident.org_id, Incident.project_id).where(Incident.id == intent.incident_id)
        ).first()
        if found is None:
            return
        # The transition's own history row: one raise per event, so a redelivered raise dedups.
        event_id = session.scalar(
            select(func.max(IncidentEvent.id)).where(
                IncidentEvent.incident_id == intent.incident_id, IncidentEvent.kind == intent.transition)
        )
    org_id, project_id = found
    with BridgedSession() as session, seam.detached(), seam.bound_to(org_id):
        triggers = list(session.scalars(
            select(TaskTrigger).where(TaskTrigger.source_kind == intent.source_kind,
                                      TaskTrigger.project_id == project_id).order_by(TaskTrigger.id)
        ))
        if not triggers:
            return
        incident = session.scalar(select(Incident).where(Incident.id == intent.incident_id))
        if incident is None:
            return
        event = templates.event(session, incident, intent.transition)
        ingest_incident(triggers, event, f"incident:{intent.incident_id}:{intent.transition}:{event_id or 0}")


# ── 3. The checks before filing ──────────────────────────────────────────────────────────────────────


def incident_id_of(row: TriggerDelivery) -> Optional[int]:
    """The incident a delivery is about; None for a hand-pasted sample (a Run now), which meets no check."""
    value = ((row.event or {}).get("incident") or {}).get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _in_window(now: datetime, quiet: dict[str, Any]) -> Optional[datetime]:
    """When the quiet window `quiet` holding `now` ends (UTC), or None when `now` is outside it. A window whose start
    is after its end spans midnight."""
    zone = ZoneInfo(quiet["tz"])
    local = now.astimezone(zone)
    start, end = (time.fromisoformat(quiet[k]) for k in ("start", "end"))
    clock = local.time().replace(tzinfo=None)
    day = local.date()
    if start < end:
        inside = start <= clock < end
        end_day = day
    else:
        inside = clock >= start or clock < end
        end_day = day + timedelta(days=1) if clock >= start else day
    if not inside:
        return None
    return datetime.combine(end_day, end, tzinfo=zone).astimezone(timezone.utc)


def _environment_ok(config: dict[str, Any], incident: Any, project: Any) -> bool:
    """Architecture §6.2: a list names the environments; null is the project's production environment for a type that
    carries one, every environment for a failed deploy (D5); a merge-lane slowdown has none."""
    if incident.type == "lane_slowdown" or incident.environment is None:
        return True
    wanted = config.get("environments")
    if wanted is None:
        if incident.type == "deploy_failed":
            return True
        from app.releases import stages

        wanted = [stages.production_environment(project) or "production"]
    return incident.environment in wanted


def admission(session: Session, trigger: TaskTrigger, row: TriggerDelivery, now: datetime) -> Optional[Verdict]:
    """Steps 1–4 of §6.3 for an incident-family delivery: why it stops here, or None to go on to filing."""
    from app.models.production_qa import Incident
    from app.models.projects import Project
    from app.production_qa import gate
    from app.production_qa.vocabulary import (
        REASON_BELOW_SEVERITY,
        REASON_ENVIRONMENT_NOT_SELECTED,
        REASON_HELD_QUIET_HOURS,
        REASON_INCIDENT_RESOLVED,
        REASON_TYPE_NOT_SELECTED,
        severity_at_least,
    )
    from app.trigger_config import SHAPES
    from app.triggers import FILTERED, HELD, SKIPPED

    incident_id = incident_id_of(row)
    if incident_id is None:
        return None
    incident = session.scalar(select(Incident).where(Incident.id == incident_id))
    if incident is None:
        return Verdict(FILTERED, "incident deleted")
    project = session.scalar(select(Project).where(Project.id == incident.project_id))
    if project is None:
        return Verdict(FILTERED, "project deleted")
    state = gate.state(session, project)
    if not state.enabled:
        return Verdict(SKIPPED, state.reason or "production_qa_off")
    config = trigger.source_config or {}
    allowed = config.get("types") or SHAPES[trigger.source_kind].TYPES  # type: ignore[attr-defined]
    if incident.type not in allowed:
        return Verdict(FILTERED, REASON_TYPE_NOT_SELECTED)
    if not _environment_ok(config, incident, project):
        return Verdict(FILTERED, REASON_ENVIRONMENT_NOT_SELECTED)
    if not severity_at_least(incident.severity, config.get("min_severity") or "error"):
        return Verdict(FILTERED, REASON_BELOW_SEVERITY)
    if config.get("quiet_hours"):
        until = _in_window(now, config["quiet_hours"])
        if until is not None:
            return Verdict(HELD, REASON_HELD_QUIET_HOURS, until)
    if incident.state == "resolved":
        return Verdict(FILTERED, REASON_INCIDENT_RESOLVED)
    return None


# Held deliveries one pass releases at most; the rest go on the next minute.
RELEASE_BATCH = 500


def release_held(session: Session, now: datetime) -> None:
    """The `release_held` cron job: every held delivery whose window ended goes back to `received` and meets every
    check again. The fleet-wide session only LISTS them; each row is written under its own trigger's org
    (`cron.py`). Wakes the processor when any was released."""
    from app.trigger_intake import fire_intake, trigger_session

    due = session.execute(
        select(TriggerDelivery.id, TriggerDelivery.trigger_id)
        .where(TriggerDelivery.state == "held", TriggerDelivery.not_before <= now)
        .order_by(TriggerDelivery.id).limit(RELEASE_BATCH)
    ).all()
    released = 0
    for delivery_id, trigger_id in due:
        with trigger_session(trigger_id) as (bound, _org):
            released += bound.execute(
                update(TriggerDelivery)
                .where(TriggerDelivery.id == delivery_id, TriggerDelivery.state == "held")
                .values(state="received", not_before=None, reason=None, processed_at=None)
                .execution_options(synchronize_session=False)
            ).rowcount
            bound.commit()
    if released:
        fire_intake()


# ── 4. The claim ─────────────────────────────────────────────────────────────────────────────────────


def _role(session: Session, trigger: TaskTrigger) -> str:
    """`fix` when the automation files a build persona (it can ship the fix), else `triage`."""
    from app.models.automations import Automation
    from app.persona_registry import REGISTRY

    params = session.scalar(select(Automation.action_params).where(Automation.id == trigger.automation_id)) or {}
    persona = REGISTRY.get(str(params.get("persona") or trigger.persona))
    return "fix" if persona is not None and persona.mode == "build" else "triage"


def _claim_live(session: Session, claim: Any) -> bool:
    """Whether an unreleased claim still stands for an open task: its task in any state but `done` (a draft counts,
    A-13), or — no task bound yet — its delivery still filing or queued. A done or deleted task, or a delivery that
    ended without one, leaves it stale."""
    from app.triggers import FILING, QUEUED, RECEIVED

    task_id = claim.task_id
    if task_id is None and claim.delivery_id is not None:
        delivery = session.execute(
            select(TriggerDelivery.state, TriggerDelivery.task_id).where(TriggerDelivery.id == claim.delivery_id)
        ).first()
        if delivery is None:
            return False
        if delivery.state in (FILING, QUEUED, RECEIVED):
            return True
        task_id = delivery.task_id
    if task_id is None:
        return False
    state = session.scalar(select(Task.state).where(Task.id == task_id))
    return state is not None and state != "done"


def pre_file(session: Session, trigger: TaskTrigger, row: TriggerDelivery, target_project_id: int) -> Optional[str]:
    """Take the incident's dedup claim under its advisory lock (held to the caller's commit); the skip reason
    (`incident_task_open`, or `REASON_OTHER_PROJECT` for an incident outside the target project), or None to file. A
    delivery naming no incident (a pasted sample) takes no claim."""
    from app.models.production_qa import Incident, IncidentTask
    from app.production_qa.vocabulary import REASON_INCIDENT_TASK_OPEN

    incident_id = incident_id_of(row)
    if incident_id is None:
        return None
    found = session.execute(select(Incident.org_id, Incident.project_id).where(Incident.id == incident_id)).first()
    if found is None:
        return None
    org_id, project_id = found
    if project_id != target_project_id:
        return REASON_OTHER_PROJECT
    session.execute(select(func.pg_advisory_xact_lock(lock_key("incident", incident_id))))
    now = utcnow()
    for claim in session.scalars(select(IncidentTask).where(
        IncidentTask.incident_id == incident_id, IncidentTask.role != "revert", IncidentTask.released_at.is_(None),
    )).all():
        if claim.delivery_id == row.id:
            return None
        if _claim_live(session, claim):
            return REASON_INCIDENT_TASK_OPEN
        claim.released_at = now
    session.flush()
    session.add(IncidentTask(org_id=org_id, incident_id=incident_id, role=_role(session, trigger), delivery_id=row.id))
    session.flush()
    return None


def post_file(session: Session, trigger: TaskTrigger, row: TriggerDelivery, task: Task) -> None:
    """Bind the delivery's claim to the task it filed (the caller commits), then the revert hook."""
    from app.models.production_qa import IncidentTask

    session.execute(
        update(IncidentTask)
        .where(IncidentTask.delivery_id == row.id, IncidentTask.released_at.is_(None),
               IncidentTask.task_id.is_(None), IncidentTask.role != "revert")
        .values(task_id=task.id)
        .execution_options(synchronize_session=False)
    )
    _file_revert(session, trigger, row, task)


# ── 5. The revert (architecture §6.6, D12, A-7) ──────────────────────────────────────────────────────

REVERT_NO_AUTOMATION = "Revert: no automation"
REVERT_NO_SUSPECT = "Revert: no suspect PR"


def _revert_prs(session: Session, incident_id: int) -> list[Any]:
    """The suspects a revert undoes: the confirmed ones, else the `high` ones; only rows naming a PR."""
    from app.models.production_qa import IncidentSuspect

    rows = session.scalars(select(IncidentSuspect).where(
        IncidentSuspect.incident_id == incident_id, IncidentSuspect.pr_number.is_not(None),
        IncidentSuspect.state != "dismissed",
    ).order_by(IncidentSuspect.repo_path, IncidentSuspect.pr_number)).all()
    confirmed = [r for r in rows if r.state == "confirmed"]
    return confirmed or [r for r in rows if r.confidence == "high"]


def _revert_open(session: Session, incident_id: int) -> bool:
    """A revert claim whose task is still open (any state but done): one revert per regression at a time."""
    from app.models.production_qa import IncidentTask

    claims = session.scalars(select(IncidentTask).where(
        IncidentTask.incident_id == incident_id, IncidentTask.role == "revert", IncidentTask.released_at.is_(None),
    )).all()
    return any(_claim_live(session, claim) for claim in claims)


def _revert_body(incident: Any, prs: list[Any]) -> str:
    from app.production_qa import templates

    lines = [
        f"Revert the change behind regression {templates.incident_ref(incident.id)} through a normal PR: open it "
        "against the integration branch and let the merge lane merge it, like any other PR.", "",
        f"**{incident.title}**", "",
        "PRs to revert:",
        *[f"- {templates._link(f'PR #{p.pr_number}', templates._pr_url(p.repo_path, p.pr_number))} ({p.repo_path}"
          + (f", Task #{p.task_id}" if p.task_id else "") + f") — {'confirmed' if p.state == 'confirmed' else p.confidence}"
          for p in prs],
    ]
    facts = incident.facts or {}
    if facts.get("metric"):
        lines += ["", *templates._regression(facts)]
    lines += ["", f"Incident: {templates.incident_ref(incident.id)} {templates.incident_url(incident.id)}"]
    return "\n".join(lines)


def _file_revert(session: Session, trigger: TaskTrigger, row: TriggerDelivery, task: Task) -> None:
    """For a `regression` incident whose project's `revert_policy` is not `off`: file ONE coder task reverting the
    confirmed suspects (else the `high` ones), attributed to the automation that filed the triage task and started by
    its owner (A-7) — `draft` under `draft`, started (`ready`) under `auto_file` — and claim it `role='revert'`.
    The revert is an ordinary task: its PR goes through the merge lane like any other; nothing here merges. A fault is
    logged and files nothing: the triage task stands."""
    from app.routes.tasks.dispatch import _start
    from app.task_lifecycle import DRAFT

    try:
        with session.begin_nested():
            start = _file_revert_unguarded(session, trigger, row)
        if start is not None:
            # Outside the savepoint: the start edge commits.
            _start(start, session, sources=(DRAFT,), run_requested=False)
    except Exception:  # noqa: BLE001 — the revert is an extra; the triage task it rides on must not fail with it
        log.exception("production-qa: filing the revert for delivery %s failed", row.id)


def _file_revert_unguarded(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> Optional[int]:
    """File the revert as a draft; the task id to start when the policy is `auto_file`, else None."""
    from app import actors, automation_actions, git_access, task_source
    from app.entity_shape import optional_uuid
    from app.events import emit_console_event
    from app.models.automations import Automation
    from app.models.production_qa import Incident, IncidentTask
    from app.models.projects import Project
    from app.routes.tasks.create import new_top_level_task

    incident_id = incident_id_of(row)
    if incident_id is None:
        return None
    incident = session.scalar(select(Incident).where(Incident.id == incident_id))
    if incident is None or incident.type != "regression":
        return None
    project = session.scalar(select(Project).where(Project.id == incident.project_id))
    automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
    if project is None or project.revert_policy == "off" or automation is None:
        return None
    if _revert_open(session, incident_id):
        return None
    prs = _revert_prs(session, incident_id)
    if not prs:
        log.info("production-qa: regression %s has no suspect PR to revert", incident_id)
        return None
    numbers = ", ".join(f"#{p.pr_number}" for p in prs)
    revert = new_top_level_task(
        project_id=project.id, source=task_source.trigger_source(trigger.source_kind), title=f"Revert: {numbers}",
        body=_revert_body(incident, prs), persona="coder", created_by_id=automation.created_by_id,
    )
    revert.org_id = project.org_id
    # Filed by the automation's owner, run as its "Run as" person, else the project/org default
    # (`git_access.project_forge_person`, as every automation's task).
    run_as_id = optional_uuid(automation_actions.task_params(automation.action_params).get("run_as_id"))
    revert.started_by_id = git_access.project_forge_person(session, project, run_as_id=run_as_id)
    revert.created_by_automation_id = automation.id
    revert.created_by_automation_name = automation.name
    actors.set_request_actor(session, automation.created_by_id)
    session.info[actors.MACHINE_ACT_SESSION_KEY] = True
    try:
        session.add(revert)
        session.flush()
        session.add(IncidentTask(org_id=incident.org_id, incident_id=incident_id, role="revert", task_id=revert.id,
                                 delivery_id=row.id))
        session.flush()
        emit_console_event(session, entity="task", id=revert.id, action="created")
    finally:
        actors.set_request_actor(session, None)
        session.info.pop(actors.MACHINE_ACT_SESSION_KEY, None)
    return revert.id if project.revert_policy == "auto_file" else None


def revert_note(session: Session, incident: Any) -> Optional[str]:
    """Why a regression incident has no revert where its project's policy asks for one: no `incident` automation
    hears regressions on the project (A-7: a revert rides that automation), or no suspect PR to revert. None when the
    policy is off, a revert was filed, or one may still be."""
    from app.models.production_qa import IncidentTask
    from app.models.projects import Project

    if incident.type != "regression":
        return None
    policy = session.scalar(select(Project.revert_policy).where(Project.id == incident.project_id))
    if policy in (None, "off"):
        return None
    if session.scalar(select(IncidentTask.incident_id).where(
        IncidentTask.incident_id == incident.id, IncidentTask.role == "revert").limit(1)) is not None:
        return None
    triggers = session.scalars(select(TaskTrigger).where(
        TaskTrigger.project_id == incident.project_id, TaskTrigger.source_kind == "incident",
        TaskTrigger.automation_id.is_not(None), TaskTrigger.paused_at.is_(None), TaskTrigger.failed_at.is_(None),
    )).all()
    if not any("regression" in ((t.source_config or {}).get("types") or ("regression",)) for t in triggers):
        return REVERT_NO_AUTOMATION
    if not _revert_prs(session, incident.id):
        return REVERT_NO_SUSPECT
    return None


def release_claim(session: Session, row: TriggerDelivery) -> None:
    """A firing that filed nothing (and owes nothing) frees its claim (the caller commits)."""
    from app.models.production_qa import IncidentTask

    session.execute(
        update(IncidentTask)
        .where(IncidentTask.delivery_id == row.id, IncidentTask.released_at.is_(None), IncidentTask.task_id.is_(None))
        .values(released_at=utcnow())
        .execution_options(synchronize_session=False)
    )


def _register() -> None:
    from app.production_qa import cron

    cron.register("release_held", release_held)


_register()
