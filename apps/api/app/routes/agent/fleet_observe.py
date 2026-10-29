"""Fleet observation the node reports: observe passes, alerts and run telemetry."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app import (
    alert_record,
    alert_relay,
    carryover,
    cron_runtime,
    edition_features,
    fleet_observe,
    node_credentials,
    node_disk_events,
    node_tmp_inode_alarm,
    task_notify,
    task_wake,
)
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.events import emit_console_event
from app.tenancy import seam
from app.node_identity import node_id_or_none, node_org
from app.models.agent_sessions import RunTelemetry
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks.slots import kill_intents_for_settled_holds, send_minted_intents
from app.routes.agent.auth import _CALLER_TASK_ID_RE, _check_token, require_caller_task

if edition_features.enabled("cloud_nodes"):
    from app import node_orders

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# ══ The state plane ══════════════════════════════════════════════════════════
# Cross-node containment is STRUCTURAL: `node_id` is REQUIRED on every write (never defaulted to the
# API's own) and every statement carries `WHERE node_id = :node_id`.

# ── The one-transaction observation ───────────────────────────────────────────

# SQLSTATEs the observation retries exactly once each; the transaction is atomic either way.
_RETRYABLE_SQLSTATES = ("40P01", "40001")


def _sqlstate(exc: BaseException) -> str:
    return str(getattr(getattr(exc, "orig", None), "sqlstate", "") or "")


def _run_settled_hold_kill_pass(session: Session, node_id: str) -> None:
    """The settled-hold reap: a slot whose exact `worktrees.task_id` points at a settled task.

    After the observation's commit, in its own transaction (lock order `worktrees` then `slot_intents`).
    Never raises into the response; rolls back first; retried ~12s later.
    """
    try:
        settled, issued = kill_intents_for_settled_holds(session, node_id)
        if settled:
            session.commit()
            # AFTER THE COMMIT: these mints ride this pass's transaction (`tasks.send_minted_intents`).
            send_minted_intents(issued)
            for intent in settled:
                log.warning(
                    "fleet-observe: %s slot %s #%s is still bound to settled task %s — recorded a "
                    "kill-intent (kill the runner, release its task-keyed lease)",
                    node_id, intent["project"], intent["number"], intent["task_id"],
                )
    except Exception as exc:  # noqa: BLE001 — a missed reap is retried next pass; never fail the response
        session.rollback()
        log.warning(
            "fleet-observe: %s settled-hold reap failed (%s) — no kill-intent recorded, retrying next pass",
            node_id, exc,
        )


@router.post("/fleet/observe", response_model=fleet_observe.ObservationOut)
@infra(Tenancy.ORG_SCOPED)
def fleet_observe_route(
    body: fleet_observe.ObservationIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> fleet_observe.ObservationOut:
    """One pass's whole observation, in ONE transaction.

    Contract (completeness gates, reported-set/complement split, lock ordering): `app/fleet_observe.py`.
    The node sends an observation ONLY when `pool_tick.ok` is true, so a degraded tick never becomes a
    snapshot. Emits ONE console event per moved family, then runs the settled-hold kill pass and the
    sighting pass (`fleet_observe.mark_sighted_slots`) after the commit, each in its own transaction.
    """
    if not body.node_id:
        raise HTTPException(
            status_code=422,
            detail="fleet/observe needs a node_id in the body: it is the scope of every write and of the "
                   "complement DELETE, and defaulting it would let one node rewrite another's rows",
        )
    for attempt in range(len(_RETRYABLE_SQLSTATES) + 1):
        try:
            result = fleet_observe.apply_observation(session, body)
            # Deletions emit too. Sentinels carry the observing node's org
            # (`routes/stream.py::_may_see_collection_sentinel`), resolved only when something moved — off the
            # pass's own stamp where it read one, the same row in the same transaction.
            moved = (
                result.changed_slots or result.worktrees.deleted
                or result.changed_runners or result.runners.deleted
                or result.changed_accounts or result.accounts.deleted
            )
            observed_org = (
                (result._node_org_id if result._node_org_id is not None else node_org(session, body.node_id))
                if moved else None
            )
            if result.changed_slots or result.worktrees.deleted:
                emit_console_event(
                    session, entity="worktree", id=0, action="upserted", org_id=observed_org,
                )
            # `runners` moves independently of the slots (a session starting/ending in an unchanged slot).
            if result.changed_runners or result.runners.deleted:
                emit_console_event(
                    session, entity="runner", id=0, action="upserted", org_id=observed_org,
                )
            # Quota is what an operator watches and the probe runs every 600s; without this the reading
            # reaches the page only on the stream-down poll.
            if result.changed_accounts or result.accounts.deleted:
                emit_console_event(
                    session, entity="account", id=0, action="upserted", org_id=observed_org,
                )
            session.commit()
            # After the commit: `emit_vanished_hold_alerts` uses its own sessions and must not alert on a
            # rolled-back reading (`fleet_observe.vanished_hold_statement`).
            if result.vanished_task_holds:
                fleet_observe.emit_vanished_hold_alerts(body.node_id, result.vanished_task_holds)
            # Same post-commit, own-session rule; one alert per login expiry, never raises.
            fleet_observe.emit_login_expiry_alerts(body.node_id, body.accounts)
            # The disk gate's effects (the pause alert, the gha poke): post-commit like the two above, so a
            # deadlock retry's rolled-back decision never alerts; never raises.
            node_disk_events.after_commit(result._disk_transition)
            # The /tmp inode alarm, the same post-commit rule; never raises.
            node_tmp_inode_alarm.after_commit(result._tmp_inode_crossing)
            # A runner vanished: wake the reclaimers now (task transitions already wake `task-pipeline`; an
            # observation change does not). After the commit; best effort and safe to over-fire (`runner_verdict`
            # holds on UNKNOWN; `crons.min_gap_seconds` floors bursts).
            if result.runner_lost_task_ids:
                log.info(
                    "fleet-observe: %s reports the runner gone for task(s) %s — waking the reclaimers "
                    "rather than waiting out the task-pipeline cadence",
                    body.node_id, sorted(result.runner_lost_task_ids),
                )
                # The sync form (this route is sync); same enqueue as a transition's wake, never raises.
                cron_runtime.fire_now_activity_sync(task_wake.PIPELINE_ACTIVITY)
            _run_settled_hold_kill_pass(session, body.node_id)
            # A self-hosted macOS/Windows machine's Add-node order is ready on its first observe. After the
            # commit, own transaction; best effort — the next pass retries. The community edition has no node orders.
            if edition_features.enabled("cloud_nodes"):
                try:
                    # The credential's own node where it names one: a node only finishes its own order.
                    observed = caller.node_id or node_id_or_none(session, body.node_id)
                    if observed is not None:
                        node_orders.on_machine_seen(session, observed)
                except Exception as exc:  # noqa: BLE001 — never fail the response
                    session.rollback()
                    log.warning("fleet-observe: %s node-order pass failed (%s), retrying next pass", body.node_id, exc)
            # AFTER THE COMMIT: `mark_seen_live` commits internally (`slot_state`'s rule) so it cannot run
            # inside `apply_observation`. Best-effort — the next pass (~5s) catches a missed sighting.
            try:
                # The pass's own resolution, one per request; a pass that resolved none (an
                # unregistered reporter) resolves here as before.
                project_ids = (
                    result._project_ids if result._project_ids is not None
                    else fleet_observe.resolve_project_ids(session, body)
                )
                fleet_observe.mark_sighted_slots(session, body.node_id, body, project_ids)
            except Exception as exc:  # noqa: BLE001 — see the comment above: never fail the response
                session.rollback()
                log.warning(
                    "fleet-observe: %s sighting pass failed (%s) — last_seen_live not advanced, retrying "
                    "next pass",
                    body.node_id, exc,
                )
            return result
        except IntegrityError:
            session.rollback()
            raise
        except DBAPIError as exc:
            session.rollback()
            state = _sqlstate(exc)
            if state not in _RETRYABLE_SQLSTATES or attempt >= len(_RETRYABLE_SQLSTATES):
                raise
            log.warning(
                "fleet-observe: %s hit SQLSTATE %s — retrying once (attempt %d)",
                body.node_id, state, attempt + 1,
            )
    raise HTTPException(status_code=503, detail="fleet/observe exhausted its deadlock retries")


# ── Alerts: the notify subject plus its dedup window, as a row and a unique key (AC6) ────────────────

# Insert/commit/relay/withdraw orchestration and the tumbling-window constant live in `app/alert_record.py`.
# This route is the HTTP boundary only — token check, validation, response shape.


# The `payload` keys only an in-process producer may set, stripped by `AlertIn._strip_task_id`: this
# credential authenticates a node, not a Task or person. `task_id` binds Slack threads and gates personal
# cards; `assigned_by_id`, `project`, `org`, `task_title`, `question_body`, `actor`, `console_url` and `agent` compose
# text the reader trusts (`alert_relay.task_parts`, `_console_card_link`), filled only by `task_notify`;
# `batch_follow_on` silences brief recipients (`alert_relay.brief_skips`).
# Module-level: pydantic makes underscore class attributes private.
_TRUSTED_ALERT_PAYLOAD_KEYS = (
    "task_id",
    "assigned_by_id",
    "project",
    "org",
    "task_title",
    "question_body",
    "actor",
    "console_url",
    "agent",
    alert_relay.BATCH_FOLLOW_ON_KEY,
)


class AlertIn(BaseModel):
    """The alert a node producer sends: what happened, about which issue — and nothing about who.

    `node_id` and `repo` are derived in `create_alert`: the node is the credential's (it decides the
    tenant), `repo` comes from `task_notify._notify_repo` off the daemon-stamped task. Stale producers still
    sending them keep working (pydantic drops unknown keys).
    """

    # Validated against `alert_relay.ALERT_KINDS`: pydantic accepts `""` for a required `str`, and an
    # empty kind is the one thing the relay refuses. A validator, not a `Literal`, keeps the single home.
    kind: str
    # The `<project>-<number>` reap identity; missing it collapses every reclaim in a repo into one alert.
    issue: Optional[str] = None
    reason: Optional[str] = None
    # Only a genuine `https://github.com/...` URL is relayed as clickable (`alert_relay._validate_notify_url`).
    # Declared explicitly or pydantic would discard it.
    url: Optional[str] = None
    # A synthetic alert self-marks so it never reaches the live channel. Top-level, not in `payload`:
    # pydantic drops unknown top-level keys and the alert would relay for real. The relay checks both.
    test: bool = False
    payload: dict[str, Any] = {}

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in alert_relay.ALERT_KINDS:
            raise ValueError(f"kind must be one of {alert_relay.ALERT_KINDS!r}, got {value!r}")
        if value in alert_relay.CP_ONLY_KINDS:
            # A page reaches every superadmin and cannot be muted, so only the CP's own relay may raise one; a
            # disk pause is the CP's own decision (`alert_relay.CP_ONLY_KINDS`).
            raise ValueError(f"kind {value!r} is raised by the control plane only")
        return value

    @field_validator("payload")
    @classmethod
    def _strip_task_id(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Drop the producer-supplied trusted, in-process-only fields — `_TRUSTED_ALERT_PAYLOAD_KEYS`.

        Stripped on the model so no `AlertIn` consumer can reintroduce them.
        """
        if not any(key in value for key in _TRUSTED_ALERT_PAYLOAD_KEYS):
            return value
        return {key: item for key, item in value.items() if key not in _TRUSTED_ALERT_PAYLOAD_KEYS}


class AlertOut(BaseModel):
    """The wire form of `alert_record.AlertOutcome` — three independent facts, not one flag that would
    mean "a row was inserted" while promising an operator was notified. `window_start` is ISO here."""

    delivered: bool
    relayed: bool
    relay_failed: bool
    window_start: str


def _caller_task_repo(session: Session, x_agent_task_id: str) -> Optional[str]:
    """The `repo` label for an alert raised on behalf of the Task the daemon's stamp names — or None.

    `task_notify._notify_repo` composes it, matching `uq_alerts_dedup`. Only numeric declarations resolve;
    the Task is read through the caller's scope.
    """
    declared = x_agent_task_id.strip()
    if not _CALLER_TASK_ID_RE.fullmatch(declared):
        return None
    task = session.get(Task, int(declared))
    if task is None:
        return None
    return task_notify._notify_repo(session.get(Project, task.project_id))


@router.post("/alerts", response_model=AlertOut)
@infra(Tenancy.OPERATOR)
def create_alert(
    body: AlertIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    x_agent_task_id: str = Header(default=""),
    session: Session = Depends(get_session),
) -> AlertOut:
    """Record one alert, then relay it to the operator channel. The unique key is the dedup window.

    `alert_record.record_alert` owns insert/commit/relay/withdraw and commits the session. Accepted
    residual: a dedup hit cannot tell delivered from in flight (bounded by the 10s relay timeout).

    Bound to its caller: node from the credential, `repo` from `_caller_task_repo`. A caller deriving no
    node gets a 422 (`alerts.org_id` is NOT NULL); CP producers call `record_alert` in-process.
    """
    if caller.node_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "an alert is attributed to the node whose credential raised it, and this credential names "
                "no node; the control plane's own producers record alerts in-process"
            ),
        )
    outcome = alert_record.record_alert(
        session,
        kind=body.kind,
        node_id=str(caller.node_id),
        repo=_caller_task_repo(session, x_agent_task_id),
        issue=body.issue,
        reason=body.reason,
        url=body.url,
        test=body.test,
        payload=body.payload,
    )
    return AlertOut(
        delivered=outcome.delivered,
        relayed=outcome.relayed,
        relay_failed=outcome.relay_failed,
        window_start=outcome.window_start.isoformat(),
    )


# ── Telemetry: the stream becomes an INSERT (AC7) ────────────────────────────────────────────────────


class TelemetryIn(BaseModel):
    run_id: str
    # The NAME on the wire; `run_telemetry.node_id` is a nullable uuid FK, resolved in the handler.
    node_id: Optional[str] = None
    org_repo: Optional[str] = None
    task_id: Optional[str] = None
    payload: dict[str, Any] = {}


@router.post("/telemetry", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def ingest_telemetry(
    body: TelemetryIn,
    x_agent_task_id: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """One run's telemetry, as a row.

    Separate from the read-only `app/routes/telemetry.py` (human auth). Upsert on `run_id` is the
    redelivery idempotency; the newest payload wins.
    """
    # The run's task, when it names one, must be the caller's (`require_caller_task`); unattributed passes.
    # A carried-over call must name its task: an unattributed row would land in the task's new org.
    if body.task_id is not None or carryover.request_task() is not None:
        require_caller_task(session, caller, x_agent_task_id, body.task_id or "")
    # A node credential derives the node, so the row is attributed to the caller whatever the body claims
    # (`caller_node_ref`'s rule). Otherwise (CP-self, insecure) the body's `node_id` is read as a uuid, failing
    # open: a value naming no row stores NULL (`node_id_or_none` says the lost attribution is accepted)
    # rather than losing every row from a misconfigured NODE_ID.
    # A carried-over call (`app/carryover.py`) is bound to its task's new org, where its own node is not
    # readable: the row is attributed to the calling node and lands in that org, the task's.
    carried = carryover.request_task() is not None
    node_uuid = caller.node_id if (carried or caller.is_derived) else node_id_or_none(session, body.node_id)
    # `org_id` named explicitly: this is a Core `pg_insert` (needed for the upsert), which the write seam
    # never sees (precedent: `fleet_verdicts.py`'s `slot_intents` insert). The executing node's org is the
    # only exact derivation (`RunTelemetry` is `Stored`). A scalar subquery keeps one round trip, and DML
    # skips the read seam so fleet-wide arms can resolve it. No well-known-org fallback
    # (`test_no_create_path_falls_back_to_a_well_known_org`): an unresolvable node yields NULL and the
    # publish is refused (NOT NULL), an intended narrowing of the fail-open policy.
    run_org = seam.bound_org() if carried else select(Node.org_id).where(Node.id == node_uuid).scalar_subquery()
    telemetry = pg_insert(RunTelemetry)
    session.execute(
        telemetry
        .values(
            run_id=body.run_id,
            node_id=node_uuid,
            org_repo=body.org_repo,
            task_id=body.task_id,
            payload=body.payload,
            org_id=run_org,
        )
        .on_conflict_do_update(
            constraint="uq_run_telemetry_run_id",
            set_={
                "payload": body.payload,
                "node_id": node_uuid,
                "task_id": body.task_id,
                # A fill, never a repoint: a row with an org keeps it (redelivered node names are self-reported); a NULL
                # row is repaired by the next publish.
                "org_id": func.coalesce(RunTelemetry.org_id, telemetry.excluded.org_id),
            },
        )
    )
    session.commit()
    return Response(status_code=204)
