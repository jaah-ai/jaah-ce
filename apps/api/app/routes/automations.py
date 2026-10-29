"""The automation REST boundary — When -> Target -> Action over the cron and
trigger children, the target rosters and preview, the action catalogue, and the event-only routes.

Authorization is exactly the crons' and triggers' (AC4.7): `@org_admin(Tenancy.CALLER_PLANE)` on every route — an
Org Owner/Admin works their own org's automations, the operator every org's. A system automation is the operator's
alone: 404 by id and absent from a tenant's list by an explicit read clause (`automation_service.visible_clause`),
never by the accident of where it is filed.

Every write goes through `app/automation_service.py`, which writes the child through the child's own writer, so a
schedule, a source, a secret and a project meet the refusals they always did. Event-only routes delegate to the
trigger routes on the automation's event child, so their behaviour is today's trigger behaviour.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import automation_actions, automation_service, org_quotas, target_resolver
from app.access_admin import refuse_foreign_org
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import Caller, Principal, current_caller
from app.authz.visibility import visible_project_clause
from app.automations import (
    STATUSES,
    TARGET_KINDS,
    TARGET_NODES,
    TARGET_PROJECTS,
    WHEN_EVENT,
    WHEN_KINDS,
    WHEN_SCHEDULE,
)
from app.entity_shape import get_or_404, iso_or_none, readable_error, StrictModel
from app.models.automations import Automation, AutomationRunTarget, Cron, CronRun, TaskTrigger
from app.models.task_extras import TaskAttachment
from app.tenancy.create_surface import named_org
from app.user_activity import record_write

router = APIRouter(prefix="/api/automations", tags=["automations"])

RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 200
#: How many of one run's targets `/runs` serves inline; `targets_total` says how many there are.
TARGETS_DEFAULT_LIMIT = 50
TARGETS_MAX_LIMIT = 500
_NO_STORE = {"Cache-Control": "no-store"}


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


class AutomationCreate(StrictModel):
    """`when` / `target` / `action` are the §6.3 shapes; the service validates them whole."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    org_id: UUID
    when: dict[str, Any]
    target: Optional[dict[str, Any]] = None
    action: dict[str, Any]
    paused: bool = False


class AutomationPatch(StrictModel):
    """Every key optional; only sent keys change. `target: null` is sent on purpose (an action with no target)."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    when: Optional[dict[str, Any]] = None
    target: Optional[dict[str, Any]] = None
    action: Optional[dict[str, Any]] = None
    paused: Optional[bool] = None


class PauseIn(StrictModel):
    reason: str = Field(default="", max_length=500)


class RunIn(StrictModel):
    """An event automation's Run now fires over this sample event (the test-render shape); a schedule ignores it."""

    sample: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sample")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        """No bigger than a webhook body may be (`triggers.MAX_BODY_BYTES`): it is stored as the delivery's event."""
        import json

        from app.triggers import MAX_BODY_BYTES

        if len(json.dumps(value, default=str).encode()) > MAX_BODY_BYTES:
            raise ValueError("the sample event is over 1 MiB")
        return value


class PreviewIn(StrictModel):
    org_id: UUID
    target: dict[str, Any]
    # The automation being edited, if any: its stored ids may dangle, labelled from its past runs.
    automation_id: Optional[UUID] = None


# ── Shared ────────────────────────────────────────────────────────────────────────────────────────────


def _org_or_reject(session: Session, org_id: UUID) -> UUID:
    """The org a request names: a tenant's own only (one uniform 422), and a real, enabled org."""
    refuse_foreign_org(org_id)
    return named_org(session, org_id)


def _event_child(session: Session, automation: Automation) -> TaskTrigger:
    child = automation_service.child_of(session, automation)
    if not isinstance(child, TaskTrigger):
        raise HTTPException(status_code=409, detail="this route is for an event automation; this one runs on a schedule")
    return child


def _with_secret(payload: dict[str, Any], secret: Optional[str]) -> dict[str, Any]:
    return {"automation": payload, "secret": secret}


# ── Catalogue, rosters, preview (literal paths before `/{automation_id}`) ─────────────────────────────


@router.get("/action-types")
@org_admin(Tenancy.CALLER_PLANE)
def list_automation_action_types(
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """The action types this caller may create (`automation_actions.creatable_for`)."""
    items = []
    for descriptor in automation_actions.creatable_for(caller.principal):
        activity = descriptor.system_activity
        items.append({
            "type": descriptor.type,
            "label": descriptor.label,
            "description": descriptor.description,
            "operator_only": descriptor.operator_only,
            "takes_target": descriptor.takes_target,
            "target_kinds": list(descriptor.target_kinds),
            "when_kinds": list(WHEN_KINDS) if descriptor.event_capable else [WHEN_SCHEDULE],
            "timeout_seconds": descriptor.timeout_seconds,
            "params_schema": activity.params if activity is not None else None,
        })
    return {"items": items, "total": len(items)}


@router.get("/targets/{kind}")
@org_admin(Tenancy.CALLER_PLANE)
def list_automation_targets(
    kind: str,
    org_id: UUID,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """The roster a Target selector offers: the org's dispatchable projects the caller can see, or exactly the
    nodes placement allows the org (AC2.3) with their state."""
    if kind not in TARGET_KINDS:
        raise HTTPException(status_code=404, detail=f"no target kind {kind!r}")
    org_id = _org_or_reject(caller.session, org_id)
    items = _visible(caller.session, caller.principal, target_resolver.universe(caller.session, org_id, kind))
    if kind == TARGET_PROJECTS:
        rows = [{"id": item.id, "name": item.name, "state": "ok"} for item in items]
    else:
        from app import node_presence

        online = {str(node_id).lower() for node_id in node_presence.connected_node_ids(caller.session)}
        rows = []
        for item in items:
            if not item.enabled:
                rows.append({"id": str(item.id), "name": item.name, "state": "disabled",
                             "detail": "Disabled — its tasks are skipped"})
            elif str(item.id).lower() not in online:
                rows.append({"id": str(item.id), "name": item.name, "state": "offline",
                             "detail": "Offline — its task waits for it"})
            else:
                rows.append({"id": str(item.id), "name": item.name, "state": "ok"})
    return {"items": rows, "total": len(rows)}


def _visible(session: Session, principal: Optional[Principal], items: list) -> list:
    """A project universe narrowed to what the caller can see (`visible_project_clause`); nodes pass through."""
    projects = [item for item in items if item.kind == TARGET_PROJECTS]
    if not projects:
        return items
    from app.models.projects import Project

    visible = set(session.scalars(
        select(Project.id).where(Project.id.in_([i.id for i in projects]), visible_project_clause(principal))
    ))
    return [item for item in items if item.kind != TARGET_PROJECTS or item.id in visible]


def _dangling_labels(
    session: Session, automation_id: Optional[UUID], kind: str, keys: list[str],
) -> list[dict[str, Any]]:
    """`label` is the last name a past run recorded for the id, else `#<id>`; `state` is `removed`."""
    names: dict[str, str] = {}
    if automation_id is not None and keys:
        for target_id, name in session.execute(
            select(AutomationRunTarget.target_id, AutomationRunTarget.target_name)
            .where(AutomationRunTarget.automation_id == automation_id, AutomationRunTarget.target_kind == kind,
                   AutomationRunTarget.target_id.in_(keys))
            .order_by(AutomationRunTarget.id)
        ).all():
            if name:
                names[target_id.lower()] = name
    out = []
    for key in keys:
        value: Any = int(key) if kind == TARGET_PROJECTS else key
        out.append({"id": value, "label": names.get(key, f"#{key}"), "state": "removed"})
    return out


@router.post("/targets/preview")
@org_admin(Tenancy.CALLER_PLANE)
def preview_automation_target(
    body: PreviewIn,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """What a rule resolves to right now — the editor's "Will run on N of M" and its list (AC3.6). Names only what
    the roster would show this caller."""
    org_id = _org_or_reject(caller.session, body.org_id)
    if body.automation_id is not None:
        automation_service.require(caller.session, caller.principal, body.automation_id)
    rule = automation_service.target_rule(body.target)
    items = _visible(caller.session, caller.principal, target_resolver.universe(caller.session, org_id, rule.kind))
    resolution = target_resolver.apply_rule(items, rule.kind, rule.mode, rule.ids)
    resolved = [
        {"id": item.id if rule.kind == TARGET_PROJECTS else str(item.id), "name": item.name}
        for item in resolution.fire
    ]
    return {
        "resolved": resolved,
        "resolved_count": len(resolved),
        "universe_count": len(items),
        "dangling": _dangling_labels(caller.session, body.automation_id, rule.kind, resolution.dangling),
    }


@router.get("/by-legacy/{kind}/{legacy_id}")
@org_admin(Tenancy.CALLER_PLANE)
def automation_by_legacy(
    kind: str,
    legacy_id: UUID,
    caller: Caller = Depends(current_caller),
) -> dict[str, str]:
    """The automation an old `/crons/:id` or `/triggers/:id` link names (the console's redirects)."""
    model = {"cron": Cron, "trigger": TaskTrigger}.get(kind)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no legacy kind {kind!r}; expected 'cron' or 'trigger'")
    automation_id = caller.session.scalar(select(model.automation_id).where(model.id == legacy_id))
    if automation_id is None:
        raise HTTPException(status_code=404, detail=f"no automation for {kind} {legacy_id}")
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    return {"id": str(automation.id)}


# ── CRUD ──────────────────────────────────────────────────────────────────────────────────────────────


@router.get("")
@org_admin(Tenancy.CALLER_PLANE)
def list_automations(
    when_kind: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """Every automation the caller may see. A tenant never sees a system row, nor counts one (AC4.4)."""
    if when_kind is not None and when_kind not in WHEN_KINDS:
        raise HTTPException(status_code=422, detail=f"when_kind: must be one of {', '.join(WHEN_KINDS)}")
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status: must be one of {', '.join(STATUSES)}")
    statement = select(Automation).where(automation_service.visible_clause(caller.principal))
    if when_kind is not None:
        statement = statement.where(Automation.when_kind == when_kind)
    now = datetime.now(timezone.utc)
    items = [
        automation_service.out(caller.session, automation, now=now)
        for automation in caller.session.scalars(statement.order_by(Automation.name, Automation.id))
    ]
    if status is not None:
        items = [item for item in items if item["status"] == status]
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
def create_automation(
    body: AutomationCreate,
    response: Response,
    background: BackgroundTasks,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """Create one automation and its When child. An event automation with a signed source answers its secret here,
    once."""
    # Before `named_org`, so a tenant naming another org gets one uniform 422 (no existence oracle).
    refuse_foreign_org(body.org_id)
    org_id = named_org(caller.session, body.org_id)
    saved = automation_service.create(
        caller.session, org_id=org_id, body=body.model_dump(), actor=caller.actor, principal=caller.principal,
        background=background,
    )
    response.headers.update(_NO_STORE)
    return _with_secret(automation_service.out(caller.session, saved.automation), saved.secret)


@router.get("/{automation_id}")
@org_admin(Tenancy.CALLER_PLANE)
def get_automation(
    automation_id: UUID,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    return automation_service.out(caller.session, automation)


@router.patch("/{automation_id}")
@org_admin(Tenancy.CALLER_PLANE)
def update_automation(
    automation_id: UUID,
    body: AutomationPatch,
    response: Response,
    background: BackgroundTasks,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """Patch one automation; the merged whole is validated. A When-kind change to an event answers its new secret."""
    who = caller.principal
    # Not locked here: the service locks the child first, the order the cron and trigger writers lock in.
    automation = automation_service.require(caller.session, who, automation_id)
    saved = automation_service.update(
        caller.session, automation, body=body.model_dump(exclude_unset=True), actor=caller.actor, principal=who,
        background=background,
    )
    if saved.secret is not None:
        response.headers.update(_NO_STORE)
    return _with_secret(automation_service.out(caller.session, saved.automation), saved.secret)


@router.delete("/{automation_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def delete_automation(
    automation_id: UUID,
    background: BackgroundTasks,
    caller: Caller = Depends(current_caller),
) -> Response:
    """Delete one automation with its When child; tasks it filed keep its name."""
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    automation_service.delete(caller.session, automation, actor=caller.actor, background=background)
    return Response(status_code=204)


@router.post("/{automation_id}/pause")
@org_admin(Tenancy.CALLER_PLANE)
def pause_automation(
    automation_id: UUID,
    background: BackgroundTasks,
    body: PauseIn = Body(default_factory=PauseIn),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    automation_service.set_paused(
        caller.session, automation, True, actor=caller.actor, background=background, reason=body.reason,
    )
    return automation_service.out(caller.session, automation)


@router.post("/{automation_id}/resume")
@org_admin(Tenancy.CALLER_PLANE)
def resume_automation(
    automation_id: UUID,
    background: BackgroundTasks,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    automation_service.set_paused(caller.session, automation, False, actor=caller.actor, background=background)
    return automation_service.out(caller.session, automation)


@router.post("/{automation_id}/release")
@org_admin(Tenancy.CALLER_PLANE)
def release_automation(
    automation_id: UUID,
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """Release a stuck schedule: end the firing that outlived its timeout and still holds the run lock, so the
    next tick runs. Its run history stays (`automation_service.release_run`)."""
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    released = automation_service.release_run(caller.session, automation, actor=caller.actor)
    return {**automation_service.out(caller.session, automation), "released": released}


def _run_now(fn, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    fn(*args, **kwargs)


def _in_background(fn, *args: Any, **kwargs: Any) -> None:
    """Run a firing off the request, on a thread of its own: a thread starts on contextvar DEFAULTS, so the
    request's tenancy binding does not follow it (each firing binds its own org). A seam tests replace to run
    inline."""
    import threading

    threading.Thread(target=_run_now, args=(fn, args, kwargs), daemon=True, name="automation-run-now").start()


@router.post("/{automation_id}/run")
@org_admin(Tenancy.CALLER_PLANE)
def run_automation(
    automation_id: UUID,
    body: Optional[RunIn] = Body(default=None),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """Run once now (cause `manual`), off the request. A schedule's firing goes through the cron wrapper (its run
    lock, bound and `cron_runs` row, as Run now always has); an event automation files over `sample` (the test
    delivery: no dedup key, so it always files). A paused automation runs too and stays paused. A system
    function or noop keeps its cron's Run now."""
    from app import automation_fire
    from app.org_gate import require_org_active
    from app.routes.crons import run_cron_now

    automation = automation_service.require(caller.session, caller.principal, automation_id)
    child = automation_service.child_of(caller.session, automation)
    fire_capable = automation_fire.fire_capable(caller.session, automation.id) is not None
    # Before the branch, so every path is gated here (`run_cron_now` keeps its own as defence in depth).
    require_org_active(caller.session, automation.org_id)
    if isinstance(child, Cron) and not fire_capable:
        fired = run_cron_now(child.id, caller.actor, caller.session)
        return {"fired": fired.fired, "detail": fired.detail}
    if not fire_capable:  # pragma: no cover - an event automation's action always has a filer
        raise HTTPException(status_code=409, detail="this automation's action cannot be run now")
    if isinstance(child, Cron):
        from app.cron_runtime import run_cron_tick

        _in_background(run_cron_tick, child.id, datetime.now(timezone.utc), cause=automation_fire.CAUSE_MANUAL)
        return {"fired": True, "detail": "Running now — the run appears in Runs when it finishes."}
    delivery_id = _manual_delivery(caller.session, child, (body or RunIn()).sample)
    caller.session.commit()
    _in_background(run_event_now, child.id, delivery_id)
    return {"fired": True, "detail": "Running now — the run appears in Runs when it finishes.",
            "delivery_id": delivery_id}


def _manual_delivery(session: Session, trigger: TaskTrigger, sample: dict[str, Any]) -> int:
    """The Run now delivery: born claimed (`filing`), so no intake pass takes it while it fires, and a crash leaves
    it to the stale-claim resume like any firing. Refused while the system holds the trigger failed."""
    from datetime import timedelta

    from app.models.automations import TriggerDelivery
    from app.trigger_intake import preview_of, redact_headers
    from app.triggers import FILING

    from app.trigger_intake import filings_in_last_hour

    now = datetime.now(timezone.utc)
    # A paused trigger runs; one the system failed is released first.
    if trigger.failed_at is not None:
        raise HTTPException(status_code=409, detail="this automation is stopped: trigger failed: "
                            f"{trigger.failed_reason or 'stopped by the system'}")
    # One firing of a trigger at a time, as the intake processor keeps it (`trigger_intake._filing_elsewhere`).
    if session.scalar(select(TriggerDelivery.id).where(
            TriggerDelivery.trigger_id == trigger.id, TriggerDelivery.state == FILING,
            TriggerDelivery.claimed_until >= now).limit(1)) is not None:
        raise HTTPException(status_code=409, detail="a run of this automation is already in progress")
    # A Run now spends the trigger's hourly budget like any delivery.
    if filings_in_last_hour(session, trigger.id, now) >= trigger.rate_limit_per_hour:
        raise HTTPException(status_code=429, detail=f"Rate capped: {trigger.rate_limit_per_hour}/hour reached")
    event = dict(sample)
    if isinstance(event.get("headers"), dict):
        event["headers"] = redact_headers(event["headers"])
    if isinstance(event.get("slack"), dict):
        # A Slack reply is aimed by the stored event (`trigger_intake._slack_thread`); a hand-written sample must
        # never aim the shared bot at a channel, so it keeps the message and loses its address.
        event["slack"] = {k: v for k, v in event["slack"].items() if k not in _SLACK_ADDRESS_KEYS}
    descriptor = automation_actions.get("task")
    row = TriggerDelivery(
        trigger_id=trigger.id, org_id=trigger.org_id, state=FILING, event=event, preview=preview_of(event),
        claimed_until=now + timedelta(seconds=descriptor.timeout_seconds),
    )
    session.add(row)
    session.flush()
    return row.id


#: The keys of a Slack event that address a reply (`trigger_intake._slack_thread`), dropped from a Run now sample.
_SLACK_ADDRESS_KEYS = frozenset({"channel", "ts", "thread_ts", "permalink"})


def run_event_now(trigger_id: UUID, delivery_id: int) -> None:
    """The background half of an event automation's Run now: fire it over its claimed delivery, in the trigger's
    org. Never raises; a failure is closed from the ledger (`automation_fire.finalize_delivery`)."""
    import logging

    from app import automation_fire
    from app.models.automations import TriggerDelivery
    from app.trigger_intake import trigger_session

    log = logging.getLogger("automations")
    try:
        with trigger_session(trigger_id) as (session, _org):
            trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id))
            automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
            row = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == delivery_id))
            try:
                automation_fire.fire(session, automation, cause=automation_fire.CAUSE_MANUAL, delivery=row,
                                     event=row.event or {})
            except Exception as exc:  # noqa: BLE001 — see the docstring
                log.exception("automation %s: Run now raised", automation.id)
                session.rollback()
                row = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == delivery_id))
                from app.triggers import FILING

                if row is not None and row.state == FILING:
                    automation_fire.finalize_delivery(session, row, error=readable_error(exc))
    except Exception:  # noqa: BLE001 — a vanished trigger: nothing to record
        log.exception("automation Run now for trigger %s could not run", trigger_id)


#: The runs list's `status` filter: every run, every run but a skip, or only runs that did work.
RUNS_STATUS_NO_SKIPS = "no_skips"
RUNS_STATUS_ACTED = "acted"
RUNS_STATUSES = (RUNS_STATUS_NO_SKIPS, RUNS_STATUS_ACTED)
RUNS_SEARCH_MAX = 200


@router.get("/{automation_id}/runs")
@org_admin(Tenancy.CALLER_PLANE)
def list_automation_runs(
    automation_id: UUID,
    limit: int = Query(default=RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    targets_limit: int = Query(default=TARGETS_DEFAULT_LIMIT, ge=1, le=TARGETS_MAX_LIMIT),
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=RUNS_SEARCH_MAX),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """The firings, newest first (AC4.6, AC7.4): a schedule's `cron_runs`, an event's deliveries. Each carries its
    cause (schedule tick / event delivery / Run now), outcome and detail, `target_counts` (outcome → reason → n),
    and its first `targets_limit` targets (`targets_total` says how many) — filed task, skipped and why, or error.
    An event run adds its delivery preview and dedup status. A run from before the fire engine has no targets and is
    served with its stored detail (AC5.6).

    `status` narrows to `no_skips` (every outcome but a skip) or `acted` (a schedule run that acted, an event
    delivery that dispatched); `q` keeps runs whose detail contains it, case-insensitively ("issued" finds the
    lease-drain passes that dispatched)."""
    from app.crons import RUN_OUTCOME_ACTED, RUN_OUTCOME_SKIPPED
    from app.models.automations import TriggerDelivery
    from app.triggers import DISPATCHED, SKIPPED

    if status is not None and status not in RUNS_STATUSES:
        raise HTTPException(status_code=422, detail=f"status: {status!r} is not one of {', '.join(RUNS_STATUSES)}")
    needle = (q or "").strip()
    automation = automation_service.require(caller.session, caller.principal, automation_id)
    child = automation_service.child_of(caller.session, automation)
    if isinstance(child, Cron):
        query = select(CronRun).where(CronRun.cron_id == child.id)
        if status == RUNS_STATUS_NO_SKIPS:
            query = query.where(CronRun.outcome != RUN_OUTCOME_SKIPPED)
        elif status == RUNS_STATUS_ACTED:
            query = query.where(CronRun.outcome == RUN_OUTCOME_ACTED)
        if needle:
            query = query.where(CronRun.detail.icontains(needle, autoescape=True))
        rows = caller.session.scalars(
            query.order_by(CronRun.fired_at.desc(), CronRun.id.desc()).offset(offset).limit(limit)
        ).all()
        items = _cron_run_items(caller.session, rows, targets_limit)
    else:
        query = _deliveries_of(child)
        if status == RUNS_STATUS_NO_SKIPS:
            query = query.where(TriggerDelivery.state != SKIPPED)
        elif status == RUNS_STATUS_ACTED:
            query = query.where(TriggerDelivery.state == DISPATCHED)
        if needle:
            query = query.where(TriggerDelivery.reason.icontains(needle, autoescape=True))
        rows = caller.session.scalars(
            query.order_by(TriggerDelivery.id.desc()).offset(offset).limit(limit)
        ).all()
        items = _delivery_items(caller.session, child, rows, targets_limit)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{automation_id}/runs/{run_id}")
@org_admin(Tenancy.CALLER_PLANE)
def get_automation_run(
    automation_id: UUID,
    run_id: str,
    targets_limit: int = Query(default=TARGETS_DEFAULT_LIMIT, ge=1, le=TARGETS_MAX_LIMIT),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """One firing by id, in the exact item shape `list_automation_runs` serves — so a pasted run URL opens the same
    record. `run_id` is the item's own `id` string: a schedule run's uuid or an event delivery's integer. A run of
    another automation, a trigger's child delivery (never a list row), an id of the wrong form or a missing one is a
    404."""
    from app.models.automations import TriggerDelivery

    automation = automation_service.require(caller.session, caller.principal, automation_id)
    child = automation_service.child_of(caller.session, automation)
    items: list[dict[str, Any]] = []
    if isinstance(child, Cron):
        cron_run_id = _parsed(UUID, run_id)
        run = None if cron_run_id is None else caller.session.scalar(
            select(CronRun).where(CronRun.cron_id == child.id, CronRun.id == cron_run_id))
        if run is not None:
            items = _cron_run_items(caller.session, [run], targets_limit)
    else:
        delivery_id = _parsed(int, run_id)
        delivery = None if delivery_id is None or not 0 < delivery_id < 2**63 else caller.session.scalar(
            _deliveries_of(child).where(TriggerDelivery.id == delivery_id))
        if delivery is not None:
            items = _delivery_items(caller.session, child, [delivery], targets_limit)
    if not items:
        raise HTTPException(status_code=404, detail="run not found")
    return items[0]


def _parsed(kind: type, raw: str) -> Any:
    """`raw` as a `kind` id, or None when it is not one — a malformed id is a run that does not exist."""
    try:
        return kind(raw)
    except ValueError:
        return None


def _deliveries_of(trigger: Any) -> Any:
    """An event automation's runs: its trigger's top-level deliveries (a child delivery is part of its parent's)."""
    from app.models.automations import TriggerDelivery

    return select(TriggerDelivery).where(TriggerDelivery.trigger_id == trigger.id,
                                         TriggerDelivery.parent_delivery_id.is_(None))


def _cron_run_items(session: Session, rows: list[Any], targets_limit: int) -> list[dict[str, Any]]:
    """A schedule's runs as the runs page serves them — the one serializer for the list and the by-id read. An
    errored run carries `failure_cause`, the short cause the list's failing row leads with; a skip its `skip_reason`."""
    from app import automation_fire
    from app.crons import RUN_OUTCOME_ERROR
    from app.entity_shape import iso_or_none

    ledger = _targets_by_run(session, AutomationRunTarget.cron_run_id, [run.id for run in rows], targets_limit)
    items: list[dict[str, Any]] = []
    for run in rows:
        detail = run.detail or run.skip_reason or ""
        items.append({
            "id": str(run.id), "cause": automation_fire.cause_of(detail, WHEN_SCHEDULE),
            "fired_at": iso_or_none(run.fired_at), "outcome": run.outcome,
            # The marker rides its own key, so the page words it as Audit > Schedule does (`cron/vocabulary.ts`).
            "detail": _unmarked(run.detail or ""), "skip_reason": run.skip_reason, "task_id": run.task_id,
            "failure_cause": automation_service.failure_cause(detail) if run.outcome == RUN_OUTCOME_ERROR else None,
            **ledger.get(run.id, _NO_TARGETS),
        })
    return items


def _delivery_items(session: Session, trigger: Any, rows: list[Any], targets_limit: int) -> list[dict[str, Any]]:
    """An event's runs as the runs page serves them, with each delivery's dedup status and, for a received message, its
    `email` (subject and sender, so a row names the message it ran on) — the one serializer for the list and the by-id
    read."""
    from app import automation_fire
    from app.entity_shape import iso_or_none
    from app.models.automations import TriggerDedup
    from app.models.notifications import InboundEmail
    from app.triggers import FAILED, OUTCOME_LABELS

    ledger = _targets_by_run(session, AutomationRunTarget.delivery_id, [row.id for row in rows], targets_limit)
    keys = [row.dedup_key for row in rows if row.dedup_key]
    reserved = {
        key: delivery for key, delivery in session.execute(
            select(TriggerDedup.dedup_key, TriggerDedup.delivery_id)
            .where(TriggerDedup.trigger_id == trigger.id, TriggerDedup.dedup_key.in_(keys))
        ).all()
    } if keys else {}
    email_ids = [row.inbound_email_id for row in rows if row.inbound_email_id]
    emails = {
        email_id: {"subject": subject, "from": sender} for email_id, subject, sender in session.execute(
            select(InboundEmail.id, InboundEmail.subject, InboundEmail.from_address)
            .where(InboundEmail.id.in_(email_ids), InboundEmail.org_id == trigger.org_id)
        ).all()
    } if email_ids else {}
    items: list[dict[str, Any]] = []
    for delivery in rows:
        reason = delivery.reason or ""
        items.append({
            "id": str(delivery.id), "cause": automation_fire.cause_of(reason, WHEN_EVENT),
            "fired_at": iso_or_none(delivery.received_at),
            "outcome": delivery.state, "outcome_label": OUTCOME_LABELS.get(delivery.state, delivery.state),
            "detail": _unmarked(reason), "task_id": delivery.task_id, "preview": delivery.preview,
            "failure_cause": automation_service.failure_cause(reason) if delivery.state == FAILED else None,
            "dedup": _dedup_status(delivery, reserved),
            "owed_targets": len(delivery.owed_targets or []),
            "email": emails.get(delivery.inbound_email_id),
            **ledger.get(delivery.id, _NO_TARGETS),
        })
    return items


def _unmarked(text: str) -> str:
    from app.automation_fire import MANUAL_MARK

    return text[len(MANUAL_MARK):] if text.startswith(MANUAL_MARK) else text


def _targets_by_run(session: Session, column, run_ids: list[Any], limit: int) -> dict[Any, dict[str, Any]]:
    """Each run's ledger summary — `target_counts` (outcome → reason → n) and `targets_total` counted in SQL, and its
    first `limit` targets — in two queries for the whole page, so an All-nodes run never loads every row."""
    from sqlalchemy import func

    if not run_ids:
        return {}
    out: dict[Any, dict[str, Any]] = {run_id: {"target_counts": {}, "targets_total": 0, "targets": []}
                                      for run_id in run_ids}
    for run_id, outcome, reason, n in session.execute(
        select(column, AutomationRunTarget.outcome, AutomationRunTarget.reason, func.count())
        .where(column.in_(run_ids)).group_by(column, AutomationRunTarget.outcome, AutomationRunTarget.reason)
    ).all():
        out[run_id]["target_counts"].setdefault(outcome, {})[reason] = n
        out[run_id]["targets_total"] += n
    ranked = (
        select(AutomationRunTarget.id, func.row_number().over(partition_by=column, order_by=AutomationRunTarget.id)
               .label("n"))
        .where(column.in_(run_ids)).subquery()
    )
    rows = session.scalars(
        select(AutomationRunTarget).join(ranked, ranked.c.id == AutomationRunTarget.id)
        .where(ranked.c.n <= limit).order_by(AutomationRunTarget.id)
    ).all()
    for row in rows:
        out[row.cron_run_id if row.cron_run_id is not None else row.delivery_id]["targets"].append({
            "kind": row.target_kind, "id": row.target_id, "name": row.target_name, "outcome": row.outcome,
            "reason": row.reason, "task_id": row.task_id,
        })
    return out


_NO_TARGETS = {"target_counts": {}, "targets_total": 0, "targets": []}


def _dedup_status(delivery: Any, reserved: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Today's dedup reading for one delivery: its key, and whether it holds the reservation (`reserved`), lost to an
    earlier one (`duplicate`), or released it (`released`: it filed nothing)."""
    from app.triggers import DEDUPED

    if not delivery.dedup_key:
        return None
    holder = reserved.get(delivery.dedup_key)
    if delivery.state == DEDUPED:
        status = "duplicate"
    elif holder == delivery.id:
        status = "reserved"
    else:
        status = "released"
    return {"key": delivery.dedup_key, "status": status}


# ── Attachments on a Task action (O3) ─────────────────────────────────────────────────────────────────


@router.post("/{automation_id}/attachments", status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
def upload_automation_attachment(
    automation_id: UUID,
    file: UploadFile = File(...),
    caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """One file onto the automation's Task action, with task upload's own limits and quotas; save its id into
    `action.params.attachment_ids` to have every fired task carry a copy."""
    from app.routes.attachments import _read_bounded, _storage_or_503, _store_whole_file, _stored_filename

    automation = automation_service.require(caller.session, caller.principal, automation_id)
    if automation.action_type != "task":
        raise HTTPException(status_code=422, detail="only a Task action carries files")
    # Asked before reading the body; the bucket is the org's (`_store_whole_file`).
    _storage_or_503(automation.id, subject="automation")
    content = _read_bounded(file.file, org_quotas.file_cap_bytes(caller.session, automation.org_id))
    attachment = TaskAttachment(
        automation_id=automation.id,
        org_id=automation.org_id,
        filename=_stored_filename(file.filename, "attachment"),
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=caller.actor.id,
    )
    _store_whole_file(caller.session, attachment, content)
    return {
        "id": attachment.id, "kind": attachment.kind, "filename": attachment.filename,
        "content_type": attachment.content_type, "size_bytes": attachment.size_bytes,
        "created_at": iso_or_none(attachment.created_at),
    }


@router.delete("/{automation_id}/attachments/{attachment_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def delete_automation_attachment(
    automation_id: UUID,
    attachment_id: int,
    caller: Caller = Depends(current_caller),
) -> Response:
    """Remove one file from the automation (and from its saved `attachment_ids`). Its bytes are reaped only once no
    fired task's copy still shares them."""
    from app.attachment_storage import reap_unreferenced_object

    automation = automation_service.require(caller.session, caller.principal, automation_id, for_update=True)
    attachment = get_or_404(
        caller.session, TaskAttachment, attachment_id, detail=f"no attachment {attachment_id} on this automation",
        where=(TaskAttachment.automation_id == automation.id,),
    )
    params = dict(automation.action_params or {})
    if attachment_id in (params.get("attachment_ids") or []):
        params["attachment_ids"] = [i for i in params["attachment_ids"] if i != attachment_id]
        automation.action_params = params
    stored_ref = attachment.s3_key
    stored_bucket = attachment.s3_bucket
    record_write(caller.session, caller.actor, "automation_update", f"Removed a file from automation {automation.name}",
                 data={"automation_id": str(automation.id), "attachment_id": attachment_id})
    caller.session.delete(attachment)
    caller.session.commit()
    if stored_ref:
        reap_unreferenced_object(caller.session, stored_ref, stored_bucket)
    return Response(status_code=204)


# ── Event-only routes: today's trigger behaviour on the automation's event child ──────────────────────


@router.post("/{automation_id}/reveal-secret")
@org_admin(Tenancy.CALLER_PLANE)
def reveal_automation_secret(
    automation_id: UUID,
    response: Response,
    caller: Caller = Depends(current_caller),
):
    from app.routes.triggers import reveal_trigger_secret

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    return reveal_trigger_secret(caller.session, child.id, response)


@router.post("/{automation_id}/rotate-secret")
@org_admin(Tenancy.CALLER_PLANE)
def rotate_automation_secret(
    automation_id: UUID,
    response: Response,
    caller: Caller = Depends(current_caller),
):
    from app.routes.triggers import rotate_trigger_secret

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    return rotate_trigger_secret(caller.session, child.id, response, caller.actor)


@router.post("/{automation_id}/test-render")
@org_admin(Tenancy.CALLER_PLANE)
def test_render_automation(
    automation_id: UUID,
    body: dict[str, Any] = Body(...),
    caller: Caller = Depends(current_caller),
):
    from app.routes.triggers import TestRenderIn, test_render_trigger

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    try:
        payload = TestRenderIn(**body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return test_render_trigger(caller.session, child.id, payload)


@router.get("/{automation_id}/deliveries")
@org_admin(Tenancy.CALLER_PLANE)
def list_automation_deliveries(
    automation_id: UUID,
    outcome: list[str] = Query(default=[]),
    before: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    caller: Caller = Depends(current_caller),
):
    from app.routes.triggers import list_trigger_deliveries

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    return list_trigger_deliveries(caller.session, child.id, outcome, before, limit)


@router.get("/{automation_id}/dedup")
@org_admin(Tenancy.CALLER_PLANE)
def list_automation_dedup(
    automation_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    caller: Caller = Depends(current_caller),
):
    from app.routes.triggers import list_trigger_dedup

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    return list_trigger_dedup(caller.session, child.id, limit, offset)


@router.delete("/{automation_id}/dedup/{dedup_key:path}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def clear_automation_dedup(
    automation_id: UUID,
    dedup_key: str,
    caller: Caller = Depends(current_caller),
) -> Response:
    from app.routes.triggers import clear_trigger_dedup

    child = _event_child(caller.session, automation_service.require(caller.session, caller.principal, automation_id))
    return clear_trigger_dedup(caller.session, child.id, dedup_key, caller.actor)
