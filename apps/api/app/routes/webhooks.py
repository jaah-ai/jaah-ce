"""An org's outgoing webhook endpoints and their delivery log — the doors. The rules are `app/webhooks/`'s.

Every door is `@org_admin` on the caller's own org (another org's endpoint is a 404). A tenant whose plan turns
`webhooks_enabled` off is refused 403 `{"code": "plan"}` on every door but the reads and DELETE, so an org whose plan
dropped the feature can still see and remove what it saved; nothing is sent meanwhile (`engine.pass_once` checks the
plan). An operator is never refused by the plan.

The secret is minted here and answered ONCE, by the create and by `rotate-secret`; no read serves it again
(`secret_set` says one is stored). A URL that is not https or not a public address is refused 422 `url_refused`
before anything is saved, and the delivery pass re-checks it before every POST. Request and response bodies under
`/api/webhooks` are never captured (`user_activity.WEBHOOKS_BODY_PREFIX`): each write records its own audit row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, NoReturn, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import db_async, plan_terms
from app.actors import actor_names, set_request_actor, stamp_actor
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import AsyncCaller, Principal, current_caller, current_principal
from app.clock import utcnow
from app.db_async import DbDep, on_session
from app.entity_shape import add_with_org_slug, iso_or_none, new_uuid7, or_404
from app.models.orgs import Org
from app.models.projects import Project
from app.models.webhooks import OrgWebhookEndpoint, WebhookDelivery
from app.user_activity import record_write
from app.webhooks import catalog, engine

router = APIRouter(tags=["webhooks"])

_NO_ENDPOINT = "no such webhook endpoint"
_NO_DELIVERY = "no such delivery"
_MAX_EVENT_TYPES = 50
_MAX_PROJECTS = 500
_DELIVERIES_DEFAULT = 50
_DELIVERIES_MAX = 200


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    url: str = Field(max_length=engine.MAX_URL_CHARS + 64)
    event_types: list[str] = Field(min_length=1, max_length=_MAX_EVENT_TYPES)
    #: None = every project.
    project_ids: Optional[list[int]] = Field(default=None, max_length=_MAX_PROJECTS)
    enabled: bool = True


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    url: Optional[str] = Field(default=None, max_length=engine.MAX_URL_CHARS + 64)
    event_types: Optional[list[str]] = Field(default=None, min_length=1, max_length=_MAX_EVENT_TYPES)
    project_ids: Optional[list[int]] = Field(default=None, max_length=_MAX_PROJECTS)
    enabled: Optional[bool] = None


def _refuse(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _require_plan(session: Session, principal: Optional[Principal]) -> None:
    """403 `{"code": "plan"}` for a tenant whose plan leaves webhooks out; the operator passes."""
    org_id = getattr(principal, "org_id", None)
    if org_id is not None and not getattr(principal, "is_operator", False):
        if not plan_terms.switch_on(session, org_id, engine.SWITCH):
            _refuse(403, "plan", plan_terms.not_on_plan(engine.SWITCH))


def _org_id(caller: AsyncCaller) -> UUID:
    """The caller's own org: every endpoint is created there (an org-less caller has nothing to administer)."""
    return or_404(getattr(caller.principal, "org_id", None), detail=_NO_ENDPOINT)


def _endpoint_or_404(session: Session, webhook_id: UUID, principal: Optional[Principal], *,
                     lock: bool = False) -> OrgWebhookEndpoint:
    """The caller's org's endpoint, or 404. The caller's plane scopes the read already; the org predicate is the
    route's own statement of it."""
    statement = select(OrgWebhookEndpoint).where(OrgWebhookEndpoint.id == webhook_id)
    org_id = getattr(principal, "org_id", None)
    if org_id is not None:
        statement = statement.where(OrgWebhookEndpoint.org_id == org_id)
    return or_404(session.scalars(statement.with_for_update() if lock else statement).one_or_none(),
                  detail=_NO_ENDPOINT)


def _delivery_or_404(session: Session, endpoint: OrgWebhookEndpoint, delivery_id: UUID) -> WebhookDelivery:
    return or_404(session.scalars(select(WebhookDelivery).where(
        WebhookDelivery.id == delivery_id, WebhookDelivery.endpoint_id == endpoint.id,
    )).one_or_none(), detail=_NO_DELIVERY)


def _last_deliveries(session: Session, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = session.execute(
        select(WebhookDelivery.endpoint_id, WebhookDelivery.state, WebhookDelivery.last_status_code,
               WebhookDelivery.last_attempt_at)
        .where(WebhookDelivery.endpoint_id.in_(ids), WebhookDelivery.last_attempt_at.is_not(None))
        .order_by(WebhookDelivery.endpoint_id, WebhookDelivery.last_attempt_at.desc())
        .distinct(WebhookDelivery.endpoint_id)
    ).all()
    return {r.endpoint_id: {"state": r.state, "status_code": r.last_status_code, "at": iso_or_none(r.last_attempt_at)}
            for r in rows}


def _out_many(session: Session, rows: list[OrgWebhookEndpoint]) -> list[dict[str, Any]]:
    names = actor_names(session, [i for r in rows for i in (r.created_by_id, r.updated_by_id)])
    last = _last_deliveries(session, [r.id for r in rows])
    return [{
        "id": str(r.id), "slug": r.slug, "name": r.name, "description": r.description, "url": r.url,
        "event_types": list(r.event_types or []),
        "project_ids": list(r.project_ids) if r.project_ids is not None else None,
        "enabled": r.enabled, "secret_set": bool(r.secret_sealed), "secret_rotated_at": iso_or_none(r.secret_rotated_at),
        "created_by": names.get(r.created_by_id), "created_at": iso_or_none(r.created_at),
        "updated_by": names.get(r.updated_by_id), "updated_at": iso_or_none(r.updated_at),
        "last_delivery": last.get(r.id),
    } for r in rows]


def _out(session: Session, row: OrgWebhookEndpoint) -> dict[str, Any]:
    return _out_many(session, [row])[0]


def _delivery_out(row: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(row.id), "event_id": row.event_id, "event_type": row.event_type, "state": row.state,
        "attempts": row.attempts, "next_attempt_at": iso_or_none(row.next_attempt_at),
        "last_status_code": row.last_status_code, "last_latency_ms": row.last_latency_ms,
        "last_error": row.last_error, "created_at": iso_or_none(row.created_at), "payload": row.payload,
    }


def _check_types(event_types: list[str]) -> list[str]:
    unknown = engine.unknown_types(event_types)
    if unknown:
        _refuse(422, "unknown_event_type", f"Unknown event type: {', '.join(unknown)}. "
                "See GET /api/webhooks/event-types.")
    return list(dict.fromkeys(event_types))


def _check_projects(session: Session, org_id: UUID, project_ids: Optional[list[int]]) -> Optional[list[int]]:
    if project_ids is None:
        return None
    wanted = list(dict.fromkeys(project_ids))
    found = set(session.scalars(select(Project.id).where(Project.org_id == org_id, Project.id.in_(wanted))).all())
    missing = [p for p in wanted if p not in found]
    if missing:
        _refuse(422, "unknown_project", f"No project {', '.join(map(str, missing))} in this organization.")
    return wanted


async def _url_or_422(url: str) -> str:
    url = url.strip()
    # The check resolves the host (DNS): off the loop, no connection held.
    why = await run_in_threadpool(engine.url_refusal, url)
    if why:
        _refuse(422, "url_refused", why)
    return url


def _audit_data(row: OrgWebhookEndpoint, **extra: Any) -> dict[str, Any]:
    """What an audit row keeps of an endpoint: its URL's origin only — a receiver often carries its own credential in
    the path or query (a chat app's hook token), and the trail is shown on the Audit page and sent on to a SIEM."""
    return {"webhook_id": row.id, "name": row.name, "url_origin": engine.url_origin(row.url),
            "event_types": list(row.event_types or []),
            "project_ids": list(row.project_ids) if row.project_ids is not None else None, "enabled": row.enabled,
            **extra}


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/webhooks/event-types")
@org_admin(Tenancy.CALLER_PLANE)
async def list_webhook_event_types() -> list[dict[str, str]]:
    """The event types an endpoint may subscribe to, in catalog order (`ping` is sent only by Send test)."""
    return [{"type": e.type, "label": e.label, "description": e.description}
            for e in catalog.event_types() if e.kinds]


@router.get("/api/webhooks")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def list_webhooks(session: Session) -> dict[str, Any]:
    """The org's endpoints, oldest first, each with its latest attempt; never a secret."""
    rows = list(session.scalars(select(OrgWebhookEndpoint).order_by(OrgWebhookEndpoint.created_at,
                                                                     OrgWebhookEndpoint.id)).all())
    return {"items": _out_many(session, rows)}


@router.get("/api/webhooks/{webhook_id}")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def get_webhook(session: Session, webhook_id: UUID,
                principal: Optional[Principal] = Depends(current_principal)) -> dict[str, Any]:
    return _out(session, _endpoint_or_404(session, webhook_id, principal))


@router.get("/api/webhooks/{webhook_id}/deliveries")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def list_webhook_deliveries(
    session: Session, webhook_id: UUID, cursor: Optional[UUID] = None,
    limit: int = Query(default=_DELIVERIES_DEFAULT, ge=1, le=_DELIVERIES_MAX),
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """The endpoint's deliveries, newest first; pass `next_cursor` as `cursor` for the next page."""
    endpoint = _endpoint_or_404(session, webhook_id, principal)
    statement = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    if cursor is not None:
        statement = statement.where(WebhookDelivery.id < cursor)
    rows = list(session.scalars(statement.order_by(WebhookDelivery.id.desc()).limit(limit + 1)).all())
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [_delivery_out(r) for r in rows], "next_cursor": str(rows[-1].id) if more else None}


# ── Writes ────────────────────────────────────────────────────────────────────────────────────────────


@router.post("/api/webhooks", status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
async def create_webhook(body: EndpointIn, caller: AsyncCaller = Depends(current_caller), *,
                         db: DbDep) -> dict[str, Any]:
    """Create an endpoint. Answers it with its `secret`, this once."""
    await db.run_sync(lambda session: _require_plan(session, caller.principal))
    await db_async.release(db)
    url = await _url_or_422(body.url)

    def work(session: Session) -> dict[str, Any]:
        set_request_actor(session, caller.actor.id)
        _require_plan(session, caller.principal)
        org_id = _org_id(caller)
        event_types = _check_types(body.event_types)
        project_ids = _check_projects(session, org_id, body.project_ids)
        count = session.scalar(select(func.count()).select_from(OrgWebhookEndpoint)
                               .where(OrgWebhookEndpoint.org_id == org_id)) or 0
        if count >= engine.MAX_ENDPOINTS_PER_ORG:
            _refuse(422, "too_many_endpoints",
                    f"An organization may have {engine.MAX_ENDPOINTS_PER_ORG} webhook endpoints. Delete one first.")
        now = utcnow()
        secret = engine.new_secret()
        row = OrgWebhookEndpoint(
            id=new_uuid7(), org_id=org_id, name=body.name.strip(), description=body.description.strip(), url=url,
            event_types=event_types, project_ids=project_ids, enabled=body.enabled,
            cursor_event_id=engine.starting_cursor(session, org_id),
        )
        engine.set_secret(row, secret, now=now, rotate=False)
        stamp_actor(row, caller.actor, created=True)
        add_with_org_slug(session, row, row.name, default="webhook")
        record_write(session, caller.actor, "webhook_created", f"Created webhook {row.name}", data=_audit_data(row))
        session.commit()
        session.refresh(row)
        return {**_out(session, row), "secret": secret}

    return await db.run_sync(work)


@router.patch("/api/webhooks/{webhook_id}")
@org_admin(Tenancy.CALLER_PLANE)
async def update_webhook(webhook_id: UUID, body: EndpointPatch, caller: AsyncCaller = Depends(current_caller), *,
                         db: DbDep) -> dict[str, Any]:
    """Change an endpoint's name, description, URL, event types, projects or enabled. Re-enabling starts from what
    happens next, not the events missed while it was off."""
    given = body.model_dump(exclude_unset=True)
    for field in ("name", "url", "event_types", "enabled"):
        if field in given and given[field] is None:
            _refuse(422, "invalid", f"{field} cannot be null.")
    def check(session: Session) -> None:
        _require_plan(session, caller.principal)
        _endpoint_or_404(session, webhook_id, caller.principal)

    await db.run_sync(check)
    await db_async.release(db)
    url = await _url_or_422(given["url"]) if "url" in given else None

    def work(session: Session) -> dict[str, Any]:
        set_request_actor(session, caller.actor.id)
        _require_plan(session, caller.principal)
        row = _endpoint_or_404(session, webhook_id, caller.principal, lock=True)
        before = _audit_data(row)
        if "event_types" in given:
            row.event_types = _check_types(given["event_types"])
        if "project_ids" in given:
            row.project_ids = _check_projects(session, row.org_id, given["project_ids"])
        if url is not None:
            row.url = url
        if "name" in given:
            row.name = given["name"].strip()
        if "description" in given:
            row.description = (given["description"] or "").strip()
        if "enabled" in given:
            if given["enabled"] and not row.enabled:
                row.cursor_event_id = engine.starting_cursor(session, row.org_id)
            row.enabled = given["enabled"]
        row.updated_at = utcnow()
        stamp_actor(row, caller.actor)
        after = _audit_data(row)
        changed = sorted(k for k in after if after[k] != before[k])
        record_write(session, caller.actor, "webhook_updated", f"Updated webhook {row.name}",
                     data={**after, "changed": changed})
        session.commit()
        session.refresh(row)
        return _out(session, row)

    return await db.run_sync(work)


@router.delete("/api/webhooks/{webhook_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def delete_webhook(session: Session, webhook_id: UUID, caller: AsyncCaller = Depends(current_caller)) -> Response:
    """Delete an endpoint and its delivery log. Open off-plan (module docstring)."""
    set_request_actor(session, caller.actor.id)
    row = _endpoint_or_404(session, webhook_id, caller.principal)
    record_write(session, caller.actor, "webhook_deleted", f"Deleted webhook {row.name}", data=_audit_data(row))
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.post("/api/webhooks/{webhook_id}/rotate-secret")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def rotate_webhook_secret(session: Session, webhook_id: UUID,
                          caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Mint a new secret and answer it, this once. For the next 24 hours each delivery also carries a
    signature by the previous secret, so the receiver can switch over without losing deliveries."""
    set_request_actor(session, caller.actor.id)
    _require_plan(session, caller.principal)
    row = _endpoint_or_404(session, webhook_id, caller.principal, lock=True)
    secret = engine.new_secret()
    now = utcnow()
    engine.set_secret(row, secret, now=now, rotate=True)
    row.updated_at = now
    stamp_actor(row, caller.actor)
    record_write(session, caller.actor, "webhook_secret_rotated", f"Rotated the secret of webhook {row.name}",
                 data={"webhook_id": row.id, "name": row.name, "previous_until": iso_or_none(row.previous_secret_until)})
    session.commit()
    session.refresh(row)
    return {**_out(session, row), "secret": secret}


async def _send_now(db: Any, caller: AsyncCaller, webhook_id: UUID, make: Any, kind: str, verb: str) -> dict[str, Any]:
    """Make (or reset) one delivery, claim it, send it off the loop, record what came back. `make(session,
    endpoint, org, now)` returns the delivery."""

    def prepare(session: Session) -> tuple[UUID, Optional[datetime], engine.Request | engine.Outcome]:
        set_request_actor(session, caller.actor.id)
        _require_plan(session, caller.principal)
        endpoint = _endpoint_or_404(session, webhook_id, caller.principal)
        now = utcnow()
        org = session.scalars(select(Org).where(Org.id == endpoint.org_id)).one()
        delivery = make(session, endpoint, org, now)
        wait = engine.manual_wait_seconds(endpoint.id)
        if wait:
            _refuse(429, "too_soon", f"A delivery was just sent to this endpoint. Wait {wait} seconds and try again.")
        record_write(session, caller.actor, kind, f"{verb} webhook {endpoint.name}",
                     data={"webhook_id": endpoint.id, "delivery_id": delivery.id, "event_type": delivery.event_type})
        built = engine.build(delivery, endpoint, now)
        claim = delivery.next_attempt_at
        session.commit()
        return delivery.id, claim, built

    delivery_id, claim, built = await db.run_sync(prepare)
    await db_async.release(db)
    outcome = built if isinstance(built, engine.Outcome) else await run_in_threadpool(engine.send, built)

    def finish(session: Session) -> dict[str, Any]:
        delivery = session.scalars(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
        ).one_or_none()
        if delivery is None:  # the endpoint was deleted while its delivery was being sent
            raise HTTPException(status_code=404, detail=_NO_ENDPOINT)
        if delivery.next_attempt_at == claim:  # still this send's: a later redelivery's result is not overwritten
            engine.record(delivery, outcome, utcnow())
        session.commit()
        session.refresh(delivery)
        return _delivery_out(delivery)

    return await db.run_sync(finish)


@router.post("/api/webhooks/{webhook_id}/test")
@org_admin(Tenancy.CALLER_PLANE)
async def test_webhook(webhook_id: UUID, caller: AsyncCaller = Depends(current_caller), *,
                       db: DbDep) -> dict[str, Any]:
    """Send a `ping` to the endpoint now and answer the delivery with what the endpoint said. A failed ping is not
    retried: Redeliver sends it again."""

    def make(session: Session, endpoint: OrgWebhookEndpoint, org: Org, now: datetime) -> WebhookDelivery:
        delivery = engine.ping(session, endpoint, org, now)
        delivery.next_attempt_at = now + engine.CLAIM_LEASE
        return delivery

    return await _send_now(db, caller, webhook_id, make, "webhook_tested", "Sent a test ping to")


@router.post("/api/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver")
@org_admin(Tenancy.CALLER_PLANE)
async def redeliver_webhook_delivery(webhook_id: UUID, delivery_id: UUID,
                                     caller: AsyncCaller = Depends(current_caller), *, db: DbDep) -> dict[str, Any]:
    """Send a delivery again now, with a fresh round of retries; the same body and `X-Jaah-Delivery`."""

    def make(session: Session, endpoint: OrgWebhookEndpoint, org: Org, now: datetime) -> WebhookDelivery:
        _delivery_or_404(session, endpoint, delivery_id)
        delivery = engine.lock_for_send(session, delivery_id)
        if delivery is None:
            _refuse(409, "sending", "This delivery is being sent right now. Check its result in a moment.")
        engine.reset_for_redelivery(delivery, now)
        return delivery

    return await _send_now(db, caller, webhook_id, make, "webhook_redelivered", "Redelivered to")
