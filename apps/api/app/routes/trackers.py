"""Tracker connections (an org admin's, `@org_admin`) and the tracker ingress (public; the adapter's `verify` is the
whole gate). The rules are `app/trackers/`'s; this module is the doors.

- `GET/POST /api/tracker-connections`, `GET/PATCH/DELETE /api/tracker-connections/{id}`,
  `GET /api/tracker-connections/{id}/containers`, `GET /api/tracker-connections/{id}/events`,
  `GET /api/tracker-connections/{id}/webhooks` (last event, and what the tracker's webhooks post to),
  `POST /api/tracker-connections/{id}/webhook-secret` (a new URL and secret, answered once);
- `POST /api/public/trackers/{tracker}/events/{connection_token}` (a tracker posting to its connection's own URL) and
  `POST /api/public/trackers/{tracker}/events` (one naming its workspace in the payload: Linear, a Jira Forge app).

A connect answers the URL, and its secrets ONCE (`webhook_secret`, `claim_code`); no read serves them again — except
the webhook check, which answers a Backlog project's derived address to an org admin where it must be pasted. A connection
the tracker posts to by its own URL can mint a new URL and secret (the old ones stop working), answered once the same
way.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import automation_service, db_async
from app.actors import set_request_actor
from app.authz.catalog import Tenancy
from app.authz.declarations import always, org_admin
from app.authz.principal import AsyncCaller, current_caller
from app.config import console_url, get_settings
from app.db_async import DbDep, on_session
from app.entity_shape import SYSTEM_ACTOR_ID, iso_or_none, or_404
from app.models.automations import Automation, TriggerDelivery
from app.models.trackers import TrackerConnection
from app.request_body import read_capped
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.trackers import connections, intake, project_links, protocol, replies, writeback
from app.trackers.vocabulary import AUTH_FORGE
from app.trackers.writeback import task_url as _console_task_url
from app.triggers import MAX_BODY_BYTES, outcome_label
from app.user_activity import record_write

log = logging.getLogger("tracker-routes")

router = APIRouter(tags=["trackers"])

_NO_CONNECTION = "no such tracker connection"
_EVENTS_LIMIT = 50
_OPERATOR_ORG_REFUSAL = (
    "org: act as the org this tracker is for, or send in_operator_org: true to connect it to the operator org"
)


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracker: str
    auth_kind: str
    credential_id: Optional[UUID] = None
    site: Optional[str] = Field(default=None, max_length=255)
    # An operator not acting as a tenant org connects into the operator org only when it says so: no client (an API
    # key, a stale tab) lands a connection there by omission.
    in_operator_org: bool = False


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: str = Field(min_length=1, max_length=255)
    project_id: int


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: Optional[list[Mapping]] = Field(default=None, max_length=500)
    start_label: Optional[str] = Field(default=None, max_length=100)
    done_state: Optional[str] = Field(default=None, max_length=100)
    allow_unmapped_users: Optional[bool] = None


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: SettingsIn


def ingress_url(conn: TrackerConnection, connection_token: Optional[str] = None) -> str:
    """Where the tracker posts: the connection's own URL (its token is answered once, at connect) or the shared one. On
    the public hooks host when this control plane has one (a tracker posts from outside dev's admin list), else the
    console's, as trigger URLs do."""
    path = f"/api/public/trackers/{conn.tracker}/events"
    hooks = get_settings().hooks_public_base_url
    base = f"{hooks}{path}" if hooks else console_url(path)
    if connections.resolves_from_payload(conn.tracker, conn.auth_kind):
        return base
    return f"{base}/{connection_token}" if connection_token else f"{base}/<token>"


def _out(conn: TrackerConnection, org: Optional[OrgRef]) -> dict[str, Any]:
    return {
        "id": str(conn.id), "name": conn.name, "tracker": conn.tracker, "auth_kind": conn.auth_kind,
        # The tenant it belongs to: an operator reads every org's, and its mapping offers only this org's projects.
        "org": org.model_dump(mode="json") if org is not None else None,
        "site": conn.site, "external_org_id": conn.external_org_id,
        "credential_id": str(conn.credential_id) if conn.credential_id else None,
        "trigger_id": str(conn.trigger_id) if conn.trigger_id else None,
        "settings": connections.settings_of(conn),
        "claimed": conn.claim_code_hash is None or conn.forge_token_sealed is not None,
        # A site admin adds this connection's webhook by hand, so New webhook URL may replace its URL and secret.
        "has_own_webhook": connections.has_own_webhook(conn),
        # Where a Jira admin installs Jaah's Forge app (this environment's link); None for any other connection.
        "install_url": (get_settings().jira_forge_install_url or None) if conn.auth_kind == AUTH_FORGE else None,
        "created_at": iso_or_none(conn.created_at), "updated_at": iso_or_none(conn.updated_at),
    }


def _out_with_automation(session: Session, conn: TrackerConnection,
                         refs: Optional[dict[UUID, OrgRef]] = None) -> dict[str, Any]:
    """One connection as the wire carries it. `refs`: a list's org labels, resolved once (`org_refs`)."""
    if refs is None:
        refs = org_refs(session, [conn.org_id])
    out = _out(conn, org_ref_of(refs, conn.org_id))
    # The Mapping tab lists the connection's primary links: the rows the project page edits too.
    out["settings"]["mappings"] = project_links.mappings_wire(session, conn.id)
    out["last_error"] = conn.last_error
    trigger = connections.trigger_of(session, conn)
    out["automation_id"] = str(trigger.automation_id) if trigger is not None and trigger.automation_id else None
    # Owned from another org, every start is refused until an Owner takes it over (the page offers it).
    automation = (session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
                  if trigger is not None and trigger.automation_id else None)
    out["owner_outside_org"] = trigger is not None and automation_service.has_foreign_owner(session, automation, trigger)
    return out


def _connection_or_404(session: Session, connection_id: UUID) -> TrackerConnection:
    return or_404(session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id)),
                  detail=_NO_CONNECTION)


def _snapshot(conn: TrackerConnection) -> TrackerConnection:
    """A transient copy of the row's columns: what `on_disconnect` is handed once the row itself is deleted, and
    `on_settings_saved` once the request's session is gone."""
    from sqlalchemy import inspect

    return TrackerConnection(**{attr.key: getattr(conn, attr.key) for attr in inspect(conn).mapper.column_attrs})


def _refused(exc: connections.ConnectionRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=f"{exc.field}: {exc.reason}")


# ── An org admin's ────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/tracker-connections")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def list_tracker_connections(session: Session) -> dict[str, Any]:
    """The org's tracker connections (never a secret), oldest first."""
    rows = session.scalars(select(TrackerConnection).order_by(TrackerConnection.created_at)).all()
    refs = org_refs(session, [row.org_id for row in rows])
    return {"connections": [_out_with_automation(session, row, refs) for row in rows]}


@router.post("/api/tracker-connections", status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
async def create_tracker_connection(
    body: ConnectionIn, caller: AsyncCaller = Depends(current_caller), *, db: DbDep,
) -> dict[str, Any]:
    """Connect a tracker: the connection, its trigger and default automation (Target = the mapped projects, none
    yet). Answers the ingress URL and, once, the secret the tracker needs (`webhook_secret` or `claim_code`).

    A connection lands in the caller's org. An operator on its own plane (not acting as an org, `app/act_as.py`) is
    refused unless `in_operator_org` says that is meant."""
    if caller.principal.is_operator and not body.in_operator_org:
        raise HTTPException(status_code=422, detail=_OPERATOR_ORG_REFUSAL)

    def work(session: Session) -> tuple[dict[str, Any], TrackerConnection]:
        set_request_actor(session, caller.actor.id)
        try:
            connected = connections.connect(
                session, caller.actor, tracker=body.tracker, auth_kind=body.auth_kind,
                credential_id=body.credential_id, site=body.site,
            )
        except connections.ConnectionRefused as exc:
            raise _refused(exc) from None
        session.commit()
        conn = connected.connection
        answer = {
            "connection": _out_with_automation(session, conn),
            "ingress_url": ingress_url(conn, connected.connection_token),
            "webhook_secret": connected.webhook_secret,
            "claim_code": connected.claim_code,
        }
        session.expunge(conn)
        return answer, conn

    answer, conn = await db.run_sync(work)
    await db_async.release(db)
    adapter = protocol.adapter_for(conn.tracker)
    if adapter is None:
        return answer

    def undo(session: Session) -> None:
        # A connection the adapter could not claim never receives an event: it, its trigger and automation go.
        set_request_actor(session, caller.actor.id)
        try:
            row = session.scalar(select(TrackerConnection).where(TrackerConnection.id == conn.id))
            if row is not None:
                connections.disconnect(session, row, caller.actor)
        except Exception:  # noqa: BLE001 — the refusal still answers; the row stays for Disconnect
            session.rollback()
            log.exception("tracker connection %s: removing the unclaimed connection failed", conn.id)

    try:
        await adapter.on_connect(conn)
    except protocol.ConnectRefused as exc:
        await db.run_sync(undo)
        raise HTTPException(status_code=422, detail=f"credential_id: {exc}") from None
    except Exception:  # noqa: BLE001 — the tracker's failure, not the caller's: logged, the connection undone
        log.exception("tracker connection %s: on_connect failed", conn.id)
        await db.run_sync(undo)
        label = connections.TRACKER_LABELS[conn.tracker]
        raise HTTPException(status_code=502, detail=f"tracker: {label} could not be reached; try again") from None

    def keep(session: Session) -> dict[str, Any]:
        # What the adapter learned (the workspace id the shared ingress resolves by) is the connection's.
        try:
            connections.keep_adapter_fields(session, conn.id, conn)
        except connections.ConnectionRefused as exc:
            session.rollback()
            undo(session)
            raise _refused(exc) from None
        session.commit()
        return _out_with_automation(session, _connection_or_404(session, conn.id))

    answer["connection"] = await db.run_sync(keep)
    return answer


@router.get("/api/tracker-connections/{connection_id}")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def get_tracker_connection(session: Session, connection_id: UUID) -> dict[str, Any]:
    return _out_with_automation(session, _connection_or_404(session, connection_id))


@router.patch("/api/tracker-connections/{connection_id}")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def update_tracker_connection(
    session: Session, connection_id: UUID, body: ConnectionPatch, background: BackgroundTasks,
    caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """Save the mappings, start label, done state or unmapped-user switch; the default automation's Target follows
    the mapped projects. After the answer, `queue_settings_saved` brings the tracker's side in step (Backlog registers
    its webhook on new projects); a failure is logged and the next save retries."""
    set_request_actor(session, caller.actor.id)
    conn = _connection_or_404(session, connection_id)
    try:
        connections.apply_settings(session, conn, caller.actor, body.settings.model_dump(exclude_unset=True,
                                                                                         mode="json"))
    except connections.ConnectionRefused as exc:
        raise _refused(exc) from None
    session.commit()
    queue_settings_saved(background, _snapshot(conn))
    return _out_with_automation(session, conn)


def queue_settings_saved(background: BackgroundTasks, conn: TrackerConnection) -> None:
    """Run the adapter's `on_settings_saved` on `conn` (detached) after the answer, when it has one: a connection's
    settings save and a project's link save are the write paths that bring the tracker's side in step."""
    hook = getattr(protocol.adapter_for(conn.tracker), "on_settings_saved", None)
    if hook is None:
        return

    async def run() -> None:
        try:
            await hook(conn)
        except Exception:  # noqa: BLE001 — the save stands; the next save retries the tracker's side
            log.exception("tracker connection %s: on_settings_saved failed", conn.id)

    background.add_task(run)


@router.post("/api/tracker-connections/{connection_id}/webhook-secret")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def rotate_tracker_webhook_secret(
    session: Session, connection_id: UUID, caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """A new webhook URL and signing secret for a connection whose webhook a site admin adds by hand (a Jira API-token
    connection, `connections.has_own_webhook`), answered once; the old URL stops working at once. 409 otherwise."""
    set_request_actor(session, caller.actor.id)
    conn = _connection_or_404(session, connection_id)
    try:
        minted = connections.new_webhook_secret(session, conn, caller.actor)
    except connections.ConnectionRefused as exc:
        raise _refused(exc) from None
    record_write(session, caller.actor, "tracker_webhook_rotated", f"New webhook URL for {conn.name}",
                 data={"connection_id": str(conn.id), **({"trigger_owner": {
                     "from": str(SYSTEM_ACTOR_ID), "to": str(caller.actor.id)}} if minted.trigger_owner_taken else {})})
    session.commit()
    return {"ingress_url": ingress_url(conn, minted.connection_token), "webhook_secret": minted.webhook_secret}


@router.delete("/api/tracker-connections/{connection_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
async def delete_tracker_connection(
    connection_id: UUID, caller: AsyncCaller = Depends(current_caller), *, db: DbDep,
) -> Response:
    """Disconnect: the connection, its trigger and automation go; the tasks it started stay."""

    def work(session: Session) -> TrackerConnection:
        set_request_actor(session, caller.actor.id)
        conn = _connection_or_404(session, connection_id)
        snapshot = _snapshot(conn)
        connections.disconnect(session, conn, caller.actor)
        return snapshot

    conn = await db.run_sync(work)
    await db_async.release(db)
    adapter = protocol.adapter_for(conn.tracker)
    if adapter is not None:
        try:
            await adapter.on_disconnect(conn)
        except Exception:  # noqa: BLE001 — Jaah's side is gone either way
            log.exception("tracker connection %s: on_disconnect failed", conn.id)
    return Response(status_code=204)


@router.get("/api/tracker-connections/{connection_id}/containers")
@org_admin(Tenancy.CALLER_PLANE)
async def list_tracker_containers(connection_id: UUID, *, db: DbDep) -> dict[str, Any]:
    """The tracker's teams/projects/spaces a mapping may name, as the tracker lists them now. 409 while the tracker
    has no adapter, or with the adapter's sentence when it cannot list them (`protocol.TrackerError`)."""

    def load(session: Session) -> TrackerConnection:
        conn = _connection_or_404(session, connection_id)
        session.expunge(conn)
        return conn

    conn = await db.run_sync(load)
    await db_async.release(db)
    adapter = protocol.adapter_for(conn.tracker)
    if adapter is None:
        raise HTTPException(status_code=409, detail=f"{conn.tracker} can't be reached yet")
    try:
        pairs = await list_containers_recorded(db, conn)
    except protocol.TrackerError as exc:
        # The adapter's sentence (no sign-in, the tracker's refusal) is the admin's reason; the Mapping tab shows it.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"containers": [{"id": cid, "name": name} for cid, name in pairs]}


async def list_containers_recorded(db: Any, conn: TrackerConnection) -> list[tuple[str, str]]:
    """The adapter's `list_containers`, its result kept on the connection: a failure's sentence as `last_error`
    (re-raised), a success clearing it and refreshing the cached container names of the connection's links. `conn`
    is detached; the caller has released `db`'s connection, so the tracker call holds none."""
    adapter = protocol.adapter_for(conn.tracker)
    if adapter is None:
        raise protocol.TrackerError(f"{connections.TRACKER_LABELS[conn.tracker]} can't be reached yet")

    def keep(session: Session, error: Optional[str], pairs: list[tuple[str, str]]) -> None:
        project_links.record_outbound(session, conn.id, error)
        if error is None:
            project_links.refresh_names(session, conn.id, pairs)
        session.commit()

    label = connections.TRACKER_LABELS[conn.tracker]
    try:
        pairs = await asyncio.wait_for(adapter.list_containers(conn), writeback.CALL_TIMEOUT_SECONDS)
    except (protocol.TrackerError, TimeoutError) as exc:
        sentence = str(exc) if isinstance(exc, protocol.TrackerError) else f"{label} didn't answer in time; try again."
        await db.run_sync(keep, sentence, [])
        raise protocol.TrackerError(sentence) from exc
    await db.run_sync(keep, None, pairs)
    return pairs


@router.get("/api/tracker-connections/{connection_id}/events")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def list_tracker_events(session: Session, connection_id: UUID) -> dict[str, Any]:
    """The connection's newest events as its trigger logged them (what each came to), newest first."""
    conn = _connection_or_404(session, connection_id)
    if conn.trigger_id is None:
        return {"events": []}
    rows = session.scalars(
        select(TriggerDelivery).where(TriggerDelivery.trigger_id == conn.trigger_id)
        .order_by(TriggerDelivery.id.desc()).limit(_EVENTS_LIMIT)
    ).all()
    return {"events": [{
        "delivery_id": row.id, "state": row.state, "outcome": outcome_label(row.state, row.reason),
        "reason": row.reason, "task_id": row.task_id, "received_at": iso_or_none(row.received_at),
        "preview": row.preview,
    } for row in rows]}


@router.get("/api/tracker-connections/{connection_id}/webhooks")
@org_admin(Tenancy.CALLER_PLANE)
async def check_tracker_webhooks(connection_id: UUID, *, db: DbDep) -> dict[str, Any]:
    """What the tracker's webhooks post to (`check_webhooks`, read live from the tracker; `[]` for a tracker that needs
    none). A tracker that does not answer in time is one `unknown`. When the connection last heard an event is the
    newest row of `list_tracker_events`: the console reads it there, so it never waits on (or outlives) this check."""

    def load(session: Session) -> tuple[TrackerConnection, list[tuple[str, str]]]:
        conn = _connection_or_404(session, connection_id)
        containers = list(dict.fromkeys(
            (row.container_id, row.container_name or row.container_id)
            for row in project_links.primary_rows(session, conn.id)))
        session.expunge(conn)
        return conn, containers

    conn, containers = await db.run_sync(load)
    await db_async.release(db)
    check = getattr(protocol.adapter_for(conn.tracker), "check_webhooks", None)
    checks: list[protocol.WebhookCheck] = []
    if check is not None:
        label = connections.TRACKER_LABELS[conn.tracker]
        try:
            checks = await asyncio.wait_for(check(conn, containers), writeback.CALL_TIMEOUT_SECONDS)
        except TimeoutError:
            checks = [protocol.WebhookCheck(conn.site or label, "unknown", f"{label} didn't answer in time; reload.")]
    return {"checks": [dataclasses.asdict(c) for c in checks]}


# ── The tracker's (public) ────────────────────────────────────────────────────────────────────────────


async def _ingest(tracker: str, connection_token: Optional[str], request: Request) -> JSONResponse:
    raw = await read_capped(request, MAX_BODY_BYTES)
    answer = await run_in_threadpool(
        intake.ingest, tracker, connection_token, raw, dict(request.headers), dict(request.query_params),
        client_ip=request.client.host if request.client else "unknown",
    )
    adapter = protocol.adapter_for(tracker)
    for connection_id, event in answer.acks:
        work = await run_in_threadpool(intake.pending_ack, connection_id, event)
        if work is None or adapter is None:
            continue
        try:
            await adapter.ack(work.connection, work.issue, _console_task_url(work.task_id), work.session_id)
        except Exception:  # noqa: BLE001 — the task is filed; a lost acknowledgement must not fail the delivery
            log.exception("tracker %s: could not acknowledge task %s", tracker, work.task_id)
            continue
        await run_in_threadpool(intake.record_ack, connection_id, work.task_id)
    for reply in answer.replies:
        # The answer flow is sync sessions all the way down (as Slack drives it): a worker thread with a loop of its
        # own, never the serving loop. What is said back goes out on the serving loop, as every adapter call does.
        notice = await run_in_threadpool(asyncio.run, replies.resolve(reply))
        conn = await run_in_threadpool(replies.connection_of, reply.connection_id) if notice is not None else None
        if conn is None or adapter is None:
            continue
        try:
            await asyncio.wait_for(adapter.post_comment(conn, reply.issue, notice, reply.session_id),
                                   writeback.CALL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — the one reply is best-effort; the delivery says what happened
            log.exception("tracker %s: could not reply on %s", tracker, reply.issue.key)
    return JSONResponse(answer.body, status_code=answer.status)


@router.post("/api/public/trackers/{tracker}/events/{connection_token}")
@always(Tenancy.BOOTSTRAP)
async def tracker_ingress_token(tracker: str, connection_token: str, request: Request) -> JSONResponse:
    """A tracker's event to its connection's own URL (Backlog, a Jira bot user). Unauthenticated at the session layer:
    the URL token finds the connection and the adapter's `verify` (its signing secret) is the gate. 401 + a logged
    `skipped` delivery when it fails; nothing is filed."""
    return await _ingest(tracker, connection_token, request)


@router.post("/api/public/trackers/{tracker}/events")
@always(Tenancy.BOOTSTRAP)
async def tracker_ingress(tracker: str, request: Request) -> JSONResponse:
    """A tracker's event naming its workspace in the payload (Linear, a Jira Forge app): the adapter finds the
    connection (`connection_key`) and `verify` is the gate, as above. An unknown workspace is the same 401 as a
    failed `verify`: the route never says which workspaces use Jaah."""
    return await _ingest(tracker, None, request)
