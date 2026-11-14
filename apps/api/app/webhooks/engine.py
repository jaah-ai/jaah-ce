"""Outgoing webhooks: how an event becomes a delivery, how a delivery is signed and sent, and the `webhooks` cron's
pass (`app/cron_system_activities.py`). The package docstring (`app/webhooks/__init__.py`) is the contract.
"""
from __future__ import annotations

import json
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Final, Iterable, Optional, Sequence
from uuid import UUID

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import credential_crypto, http_bounded, outbound_post, plan_terms
from app.clock import utcnow
from app.models.audit import ActivityEvent
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.webhooks import OrgWebhookEndpoint, WebhookDelivery
from app.webhooks import catalog

log = logging.getLogger("app.webhooks")

#: The plan switch (`plan_terms.SWITCHES`), ON on every plan.
SWITCH: Final = "webhooks_enabled"
#: The system cron's activity type.
ACTIVITY: Final = "webhooks"

PENDING: Final = "pending"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"

EVENT_HEADER: Final = "X-Jaah-Event"
DELIVERY_HEADER: Final = "X-Jaah-Delivery"
TIMESTAMP_HEADER: Final = "X-Jaah-Timestamp"
SIGNATURE_HEADER: Final = "X-Jaah-Signature"

#: How old an event must be before a pass turns it into deliveries: a row whose transaction commits late is not
#: stepped over by a cursor already past it (the SIEM export's reason, a shorter bound).
SETTLE: Final = timedelta(seconds=10)
#: The wait after attempt N fails (N = 1..6); attempt 7 failing marks the delivery `failed`.
BACKOFF: Final = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2),
                  timedelta(hours=6), timedelta(hours=12))
MAX_ATTEMPTS: Final = len(BACKOFF) + 1
#: Deliveries older than this are deleted by the pass.
RETENTION_DAYS: Final = 30
#: Activity rows read per endpoint per pass, deliveries sent per endpoint per pass, deliveries deleted per pass.
SCAN_BATCH: Final = 500
SEND_PER_ENDPOINT: Final = 20
PRUNE_BATCH: Final = 5000
#: A claimed delivery is not claimed again for this long: the pass that holds it is sending it.
CLAIM_LEASE: Final = timedelta(minutes=2)
#: A pass stops starting new POSTs after this long, inside the cron's 50 s timeout (one POST is bounded by
#: `outbound_post.POST_TIMEOUT_SECONDS` across all its addresses).
PASS_BUDGET_SECONDS: Final = 35.0
#: One org's share of a pass, across all its endpoints: it starts no new POST after this long, so one slow org
#: cannot starve the rest.
ORG_TURN_SECONDS: Final = 8.0
#: An answer slower than this ends its org's turn for the pass, as no answer does.
SLOW_SECONDS: Final = 3.0
#: How long the address check's DNS lookup may take before the send counts as unreachable.
RESOLVE_TIMEOUT_SECONDS: Final = 5.0
#: How long the previous secret still signs after a rotation.
ROTATION_OVERLAP: Final = timedelta(hours=24)
#: How often one endpoint may be sent a test ping or a redelivery from the console.
MANUAL_EVERY_SECONDS: Final = 10.0
MAX_ENDPOINTS_PER_ORG: Final = 50
MAX_URL_CHARS: Final = outbound_post.MAX_URL_CHARS

_SECRET_PREFIX = "whsec_"
_ENVELOPE = credential_crypto.RowEnvelope(b"org-webhook-endpoint-secret|", "org_webhook_endpoints")
_PREVIOUS = credential_crypto.RowEnvelope(b"org-webhook-endpoint-previous-secret|", "org_webhook_endpoints")


# ── The secret ────────────────────────────────────────────────────────────────────────────────────────


def new_secret() -> str:
    return _SECRET_PREFIX + secrets.token_urlsafe(32)


def set_secret(row: OrgWebhookEndpoint, secret: str, *, now: datetime, rotate: bool) -> None:
    """Seal `secret` as the row's; on a rotation the old one keeps signing until `ROTATION_OVERLAP` has passed."""
    if rotate:
        row.previous_secret_sealed = _PREVIOUS.seal(row.id, {"secret": _ENVELOPE.open(row.secret_sealed, row.id)["secret"]})
        row.previous_secret_until = now + ROTATION_OVERLAP
        row.secret_rotated_at = now
    row.secret_sealed = _ENVELOPE.seal(row.id, {"secret": secret})


def signing_secrets(row: OrgWebhookEndpoint, now: datetime) -> list[str]:
    """The secrets a delivery is signed with now: the current one, then the previous one while it overlaps."""
    out = [_ENVELOPE.open(row.secret_sealed, row.id)["secret"]]
    if row.previous_secret_sealed and row.previous_secret_until and row.previous_secret_until > now:
        out.append(_PREVIOUS.open(row.previous_secret_sealed, row.id)["secret"])
    return out


def signature(secrets_: Sequence[str], timestamp: int, body: bytes) -> str:
    """`sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>` per secret, comma-separated."""
    from app.trigger_secrets import sign

    signed = f"{timestamp}.".encode() + body
    return ",".join(sign(secret, signed) for secret in secrets_)


# ── What a save must hold ─────────────────────────────────────────────────────────────────────────────


def url_refusal(url: str) -> Optional[str]:
    """Why `url` cannot be an endpoint (https, a public address — `outbound_post.url_refusal` — and no `user:password@`,
    which would ride every request and every audit read), or None. Resolves DNS: call it off the event loop."""
    try:
        if httpx.URL(url.strip()).userinfo:
            return "Remove the user name and password from the address; Jaah signs each delivery instead."
    except (httpx.InvalidURL, ValueError, UnicodeError):
        pass  # `outbound_post.url_refusal` names an unreadable address
    return outbound_post.url_refusal(url)


def url_origin(url: str) -> str:
    """`scheme://host[:port]` of `url`: what an audit row keeps (a receiver's path or query may hold its own
    credential)."""
    origin = http_bounded.origin(url)
    if origin is None:
        return ""
    scheme, host, port = origin
    return f"{scheme}://{host}" + ("" if port == http_bounded.DEFAULT_PORTS.get(scheme) else f":{port}")


def unknown_types(event_types: Iterable[str]) -> list[str]:
    allowed = set(catalog.subscribable())
    return sorted({t for t in event_types if t not in allowed})


def starting_cursor(session: Session, org_id: UUID) -> int:
    """A new (or re-enabled) endpoint receives what happens next, not the org's history."""
    return outbound_post.starting_cursor(session, org_id)


# ── Event → delivery ──────────────────────────────────────────────────────────────────────────────────


def _task_id_of(kind: str, data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    raw = data.get("id") if kind.startswith("task:") else data.get("task_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _TaskFacts:
    id: int
    title: Optional[str]
    state: str
    project_id: Optional[int]


def _tasks(session: Session, org_id: UUID, ids: set[int]) -> dict[int, _TaskFacts]:
    if not ids:
        return {}
    rows = session.execute(
        select(Task.id, Task.title, Task.state, Task.project_id).where(Task.org_id == org_id, Task.id.in_(ids))
    ).all()
    return {r.id: _TaskFacts(r.id, r.title, r.state, r.project_id) for r in rows}


def _projects(session: Session, org_id: UUID, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = session.execute(select(Project.id, Project.name).where(Project.org_id == org_id, Project.id.in_(ids)))
    return {r.id: r.name for r in rows}


def payload(event_type: str, created_at: datetime, org: Org, project: Optional[tuple[int, str]],
            data: dict[str, Any]) -> dict[str, Any]:
    """The body's object less its `id` (the delivery's, added at send): ids, titles and URLs only — never a task
    body or a transcript."""
    return {
        "type": event_type,
        "created_at": created_at.isoformat(),
        "org": {"id": str(org.id), "slug": org.slug},
        "project": {"id": project[0], "name": project[1]} if project else None,
        "data": data,
    }


def _event_data(kind: str, data: Any, task: Optional[_TaskFacts], task_id: Optional[int]) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    if task_id is not None:
        from app.trackers.writeback import task_url

        out["task"] = {
            "id": task_id,
            "title": task.title if task else None,
            "state": (raw.get("state") if kind.startswith("task:") else None) or (task.state if task else None),
            "url": task_url(task_id),
        }
    if kind.startswith("pr:"):
        out["pull_request"] = {"url": raw.get("pr_url"), "number": raw.get("pr_number")}
        if raw.get("merge_sha"):
            out["pull_request"]["merge_sha"] = raw["merge_sha"]
    return out


def _settled(rows: Sequence[Any], now: datetime) -> list[Any]:
    """The rows up to, not including, the first younger than `SETTLE` in id order (the SIEM export's `settled`)."""
    cutoff = now - SETTLE
    for index, row in enumerate(rows):
        if row.created_at >= cutoff:
            return list(rows[:index])
    return list(rows)


def advance(session: Session, endpoint: OrgWebhookEndpoint, org: Org, now: datetime) -> int:
    """Turn the org's settled events past the endpoint's cursor into its deliveries (the chosen types, in its
    projects), then move the cursor past them. Unique `(endpoint_id, event_id)`: a pass that reads an event twice
    inserts it once. Returns the deliveries inserted. The caller commits."""
    by_kind = catalog.type_by_kind()
    rows = session.execute(
        select(ActivityEvent.id, ActivityEvent.kind, ActivityEvent.data, ActivityEvent.created_at)
        .where(ActivityEvent.org_id == endpoint.org_id, ActivityEvent.id > endpoint.cursor_event_id,
               ActivityEvent.kind.in_(list(by_kind)))
        .order_by(ActivityEvent.id)
        .limit(SCAN_BATCH)
    ).all()
    rows = _settled(rows, now)
    if not rows:
        return 0
    chosen = set(endpoint.event_types or ())
    wanted = [r for r in rows if by_kind.get(r.kind) in chosen]
    tasks = _tasks(session, endpoint.org_id, {t for r in wanted if (t := _task_id_of(r.kind, r.data)) is not None})
    projects = _projects(session, endpoint.org_id, {t.project_id for t in tasks.values() if t.project_id is not None})
    inserted = 0
    for r in wanted:
        task_id = _task_id_of(r.kind, r.data)
        task = tasks.get(task_id) if task_id is not None else None
        project_id = task.project_id if task else None
        if endpoint.project_ids is not None and project_id not in endpoint.project_ids:
            continue
        project = (project_id, projects.get(project_id, "")) if project_id is not None else None
        body = payload(by_kind[r.kind], r.created_at, org, project, _event_data(r.kind, r.data, task, task_id))
        made = session.scalar(
            insert(WebhookDelivery)
            .values(org_id=endpoint.org_id, endpoint_id=endpoint.id, event_id=r.id, event_type=by_kind[r.kind],
                    payload=body, next_attempt_at=now)  # `state` is the column's default, `pending`
            .on_conflict_do_nothing(constraint="uq_webhook_deliveries_endpoint_event")
            .returning(WebhookDelivery.id)
        )
        inserted += made is not None
    endpoint.cursor_event_id = int(rows[-1].id)
    return inserted


def ping(session: Session, endpoint: OrgWebhookEndpoint, org: Org, now: datetime) -> WebhookDelivery:
    """A test delivery (`ping`, no event), pending now. The caller sends it."""
    row = WebhookDelivery(
        org_id=endpoint.org_id, endpoint_id=endpoint.id, event_id=None, event_type=catalog.PING,
        payload=payload(catalog.PING, now, org, None, {"endpoint": {"id": str(endpoint.id), "slug": endpoint.slug}}),
        state=PENDING, attempts=0, next_attempt_at=now,
    )
    session.add(row)
    session.flush()
    return row


# ── One POST ──────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Request:
    """A signed POST, ready to dial: built inside a session, dialled outside it."""

    url: str
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class Outcome:
    """What one attempt came to: `status` None when no response came back."""

    ok: bool
    status: Optional[int]
    latency_ms: Optional[int]
    error: Optional[str]


def body_of(delivery: WebhookDelivery) -> bytes:
    return json.dumps({"id": str(delivery.id), **delivery.payload}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def build(delivery: WebhookDelivery, endpoint: OrgWebhookEndpoint, now: datetime) -> Request | Outcome:
    """The signed request for `delivery`, or the Outcome that stops it before any dial (a secret that will not
    open)."""
    body = body_of(delivery)
    timestamp = int(now.timestamp())
    try:
        signed = signature(signing_secrets(endpoint, now), timestamp, body)
    except (ValueError, KeyError):
        return Outcome(ok=False, status=None, latency_ms=None,
                       error="The endpoint's secret could not be opened. Rotate it.")
    return Request(url=endpoint.url.strip(), body=body, headers={
        "Content-Type": "application/json", "User-Agent": "Jaah-Webhooks/1",
        EVENT_HEADER: delivery.event_type, DELIVERY_HEADER: str(delivery.id),
        TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: signed,
    })


def _vet_bounded(url: str) -> Optional[tuple[Optional[str], tuple[str, ...]]]:
    """`http_bounded.vetted_addresses(url)`, or None when its DNS lookup takes longer than `RESOLVE_TIMEOUT_SECONDS`.
    On a thread of its own, never a shared pool: a nameserver that never answers holds only its own lookup's thread
    (until the resolver gives up), so one org's stuck names cannot queue another org's sends."""
    result: list[tuple[Optional[str], tuple[str, ...]]] = []
    lookup = threading.Thread(target=lambda: result.append(http_bounded.vetted_addresses(url)),
                              name="webhooks-resolve", daemon=True)
    lookup.start()
    lookup.join(RESOLVE_TIMEOUT_SECONDS)
    return result[0] if result else None


def _dial(url: str, body: bytes, headers: dict[str, str], addresses: Sequence[str]) -> tuple[int, bytes]:
    """The network seam (tests patch it): `outbound_post`'s bounded POST to the vetted addresses, no redirects."""
    return outbound_post.post_raw(url, body, headers, addresses)


def send(request: Request) -> Outcome:
    """Re-check the URL (https, public — a rebinding name cannot swap in a private address: only what was vetted
    is dialled), POST, time it. Never raises."""
    from app.secret_redact import redact as redact_text

    refusal = outbound_post.form_refusal(request.url)
    addresses: tuple[str, ...] = ()
    if refusal is None:
        vetted = _vet_bounded(request.url)
        if vetted is None:
            return Outcome(ok=False, status=None, latency_ms=None, error=(
                f"The endpoint's name did not resolve within {RESOLVE_TIMEOUT_SECONDS:.0f} seconds. Check its DNS."))
        why, addresses = vetted
        refusal = outbound_post.NOT_PUBLIC if why or not addresses else None
    if refusal:
        return Outcome(ok=False, status=None, latency_ms=None, error=refusal)
    started = time.monotonic()
    try:
        status, answer = _dial(request.url, request.body, request.headers, addresses)
    except (httpx.HTTPError, TimeoutError, OSError):
        return Outcome(ok=False, status=None, latency_ms=int((time.monotonic() - started) * 1000), error=(
            f"Could not reach the endpoint within {outbound_post.POST_TIMEOUT_SECONDS:.0f} seconds. "
            "Check it is up and reachable from the internet."
        ))
    except Exception as exc:  # noqa: BLE001 - a malformed URL must still land on the row as an error
        log.warning("webhooks: POST raised %s", type(exc).__name__)
        return Outcome(ok=False, status=None, latency_ms=None,
                       error=f"The request could not be sent ({type(exc).__name__}). Check the URL.")
    latency = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300:
        return Outcome(ok=True, status=status, latency_ms=latency, error=None)
    snippet, _ = redact_text(outbound_post.answer_snippet(answer))
    return Outcome(ok=False, status=status, latency_ms=latency,
                   error=f"The endpoint answered HTTP {status}" + (f": {snippet}" if snippet else "."))


def record(delivery: WebhookDelivery, outcome: Outcome, now: datetime) -> None:
    """One attempt onto the row: 2xx succeeds; otherwise retry after `BACKOFF`, and after `MAX_ATTEMPTS` fail. A
    failed `ping` is not retried: it answered the person who pressed Send test already."""
    delivery.attempts += 1
    delivery.last_attempt_at = now
    delivery.last_status_code = outcome.status
    delivery.last_latency_ms = outcome.latency_ms
    delivery.last_error = outcome.error
    if outcome.ok:
        delivery.state, delivery.next_attempt_at = SUCCEEDED, None
    elif delivery.attempts >= MAX_ATTEMPTS or delivery.event_type == catalog.PING:
        delivery.state, delivery.next_attempt_at = FAILED, None
    else:
        delivery.state, delivery.next_attempt_at = PENDING, now + BACKOFF[delivery.attempts - 1]


def reset_for_redelivery(delivery: WebhookDelivery, now: datetime) -> None:
    """Redeliver: a fresh round of attempts, the first now. The previous attempt's result stays until the next."""
    delivery.state, delivery.attempts, delivery.next_attempt_at = PENDING, 0, now + CLAIM_LEASE


# ── Manual sends (Send test, Redeliver) ───────────────────────────────────────────────────────────────

_last_manual: dict[UUID, float] = {}
_manual_lock = threading.Lock()


def clear_manual_limit() -> None:
    _last_manual.clear()


def manual_wait_seconds(endpoint_id: UUID) -> int:
    """Seconds until the endpoint may be sent another manual delivery (0 = now), and the send is counted. Per API
    process, which is the whole API (the SIEM export's test limit's reason)."""
    with _manual_lock:
        now = time.monotonic()
        last = _last_manual.get(endpoint_id)
        if last is not None and now - last < MANUAL_EVERY_SECONDS:
            return int(MANUAL_EVERY_SECONDS - (now - last)) + 1
        _last_manual[endpoint_id] = now
        return 0


# ── The nudge ─────────────────────────────────────────────────────────────────────────────────────────

_nudge_lock = threading.Lock()
#: The earliest unfired wake (0 = none) and the latest one owed: a busy fleet fires every `SETTLE` + 1 s at most,
#: and a steady stream of events can never postpone a wake already owed.
_nudge_next: float = 0.0
_nudge_last: float = 0.0
_nudge_running = False


def nudge_after_commit(session: Session) -> None:
    """A webhook-relevant event is being written: once it commits, fire the `webhooks` cron just after `SETTLE`, so
    a delivery leaves within seconds rather than at the next minute (the cron's interval is the backstop; a nudge
    lost to a restart costs latency only). Nudges coalesce into one thread."""
    from app import after_commit

    after_commit.on_commit(session, _schedule, key="webhooks-nudge")


def _schedule() -> None:
    global _nudge_next, _nudge_last, _nudge_running
    due = time.monotonic() + SETTLE.total_seconds() + 1.0
    with _nudge_lock:
        _nudge_last = max(_nudge_last, due)
        if not _nudge_next:
            _nudge_next = due
        if _nudge_running:
            return
        _nudge_running = True
    threading.Thread(target=_nudge_loop, name="webhooks-nudge", daemon=True).start()


def _nudge_loop() -> None:
    """Fire at the earliest owed wake; if more were owed after that fire, fire again at the latest of them."""
    global _nudge_next, _nudge_last, _nudge_running
    import asyncio

    from app import cron_runtime

    while True:
        with _nudge_lock:
            if not _nudge_next:
                _nudge_running = False
                return
            wait = _nudge_next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        fired_at = time.monotonic()
        try:
            asyncio.run(cron_runtime.fire_now_activity(ACTIVITY))
        except Exception:  # noqa: BLE001 - the interval is the backstop
            log.exception("webhooks: nudge could not fire the cron")
        with _nudge_lock:
            # Owed after this fire's cutoff (an event newer than `fired_at - SETTLE - 1`): one more wake, at the latest.
            _nudge_next = _nudge_last if _nudge_last > fired_at else 0.0


# ── The cron's pass ───────────────────────────────────────────────────────────────────────────────────


@dataclass
class Report:
    queued: int = 0
    sent: int = 0
    failed_attempts: int = 0
    pruned: int = 0

    @property
    def acted(self) -> bool:
        return bool(self.queued or self.sent or self.failed_attempts or self.pruned)

    def summary(self) -> str:
        if not self.acted:
            return "no webhook delivery to make"
        return (f"{self.queued} delivery(ies) queued, {self.sent} sent, {self.failed_attempts} attempt(s) failed, "
                f"{self.pruned} old delivery(ies) deleted")


def _claim(session: Session, endpoint_id: UUID, now: datetime) -> list[UUID]:
    """The endpoint's due deliveries, claimed for `CLAIM_LEASE` under `FOR UPDATE SKIP LOCKED`: a concurrent pass
    neither waits on nor sends them. The caller commits."""
    ids = list(session.scalars(
        select(WebhookDelivery.id)
        .where(WebhookDelivery.endpoint_id == endpoint_id, WebhookDelivery.state == PENDING,
               WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(SEND_PER_ENDPOINT)
        .with_for_update(skip_locked=True)
    ).all())
    if ids:
        session.execute(
            update(WebhookDelivery).where(WebhookDelivery.id.in_(ids)).values(next_attempt_at=now + CLAIM_LEASE)
        )
    return ids


def lock_for_send(session: Session, delivery_id: UUID) -> Optional[WebhookDelivery]:
    """The delivery, locked `FOR UPDATE NOWAIT` for the length of one send, or None while another send holds it:
    a redelivery and a pass never send one delivery at once, nor record over each other."""
    from sqlalchemy.exc import OperationalError

    try:
        return session.scalars(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update(nowait=True)
        ).one_or_none()
    except OperationalError:
        session.rollback()
        return None


def deliver_claimed(session: Session, endpoint: OrgWebhookEndpoint, ids: Sequence[UUID], report: Report, *,
                    claimed_until: datetime, now: Callable[[], datetime], deadline: Callable[[], bool]) -> None:
    """Send each claimed delivery under its row lock, committing after each. A delivery a redelivery took meanwhile
    (its `next_attempt_at` is no longer this pass's claim) is skipped. The first send that reaches nothing, or answers
    slower than `SLOW_SECONDS`, ends the endpoint's turn (a tarpit costs one timeout a pass, not the pass); what is
    left is released."""
    for index, delivery_id in enumerate(ids):
        if deadline():
            session.execute(update(WebhookDelivery).where(WebhookDelivery.id.in_(list(ids[index:])),
                                                          WebhookDelivery.next_attempt_at == claimed_until)
                            .values(next_attempt_at=now()))
            session.commit()
            return
        delivery = lock_for_send(session, delivery_id)
        if delivery is None or delivery.state != PENDING or delivery.next_attempt_at != claimed_until:
            session.rollback()
            continue
        built = build(delivery, endpoint, now())
        outcome = built if isinstance(built, Outcome) else send(built)
        record(delivery, outcome, now())
        session.commit()
        if outcome.ok:
            report.sent += 1
            continue
        report.failed_attempts += 1
        if outcome.status is None or (outcome.latency_ms or 0) > SLOW_SECONDS * 1000:
            rest = list(ids[index + 1:])
            if rest:
                session.execute(update(WebhookDelivery).where(WebhookDelivery.id.in_(rest),
                                                              WebhookDelivery.next_attempt_at == claimed_until)
                                .values(next_attempt_at=now()))
                session.commit()
            return


def prune(session: Session, now: datetime) -> int:
    """Delete up to `PRUNE_BATCH` deliveries older than `RETENTION_DAYS`. The caller commits."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    old = select(WebhookDelivery.id).where(WebhookDelivery.created_at < cutoff).limit(PRUNE_BATCH).scalar_subquery()
    return len(session.scalars(
        delete(WebhookDelivery).where(WebhookDelivery.id.in_(old)).returning(WebhookDelivery.id)
    ).all())


def _lock_endpoint(session: Session, endpoint_id: UUID) -> Optional[OrgWebhookEndpoint]:
    """The enabled endpoint, locked `FOR UPDATE SKIP LOCKED`: two passes never advance one cursor at once."""
    return session.scalars(
        select(OrgWebhookEndpoint)
        .where(OrgWebhookEndpoint.id == endpoint_id, OrgWebhookEndpoint.enabled.is_(True))
        .with_for_update(skip_locked=True)
    ).one_or_none()


def _turn(pass_deadline: Callable[[], bool], spent: dict[UUID, float], org_id: UUID) -> Callable[[], bool]:
    """An endpoint's share of the pass: what is left of its org's `ORG_TURN_SECONDS` (`spent` carries the org's
    sending time across its endpoints), never past the pass's own deadline."""
    started = time.monotonic() - spent.get(org_id, 0.0)
    return lambda: pass_deadline() or time.monotonic() - started > ORG_TURN_SECONDS


def pass_once(session: Session, report: Report, *, now: Callable[[], datetime], deadline: Callable[[], bool]) -> None:
    """One pass over every enabled endpoint, orgs in a fresh random order (no org always goes first), each org
    sending for at most its turn across its endpoints: advance, then send what is due. An endpoint of an org whose plan has webhooks off
    only moves its cursor to now, so turning the switch back on does not replay what happened meanwhile."""
    report.pruned += prune(session, now())
    session.commit()
    rows = session.execute(select(OrgWebhookEndpoint.id, OrgWebhookEndpoint.org_id)
                           .where(OrgWebhookEndpoint.enabled.is_(True))).all()
    session.rollback()
    # Orgs in a fresh random order, each org's endpoints together: an org with many endpoints is no likelier to go
    # first than one with a single endpoint.
    by_org: dict[UUID, list[UUID]] = {}
    for endpoint_id, org_id in rows:
        by_org.setdefault(org_id, []).append(endpoint_id)
    orgs = list(by_org)
    random.shuffle(orgs)
    ids = [endpoint_id for org_id in orgs for endpoint_id in random.sample(by_org[org_id], len(by_org[org_id]))]
    switch: dict[UUID, bool] = {}
    spent: dict[UUID, float] = {}
    for endpoint_id in ids:
        if deadline():
            break
        try:
            endpoint = _lock_endpoint(session, endpoint_id)
            if endpoint is None:
                session.rollback()
                continue
            if endpoint.org_id not in switch:
                switch[endpoint.org_id] = plan_terms.switch_on(session, endpoint.org_id, SWITCH)
            if not switch[endpoint.org_id]:
                endpoint.cursor_event_id = max(endpoint.cursor_event_id, starting_cursor(session, endpoint.org_id))
                session.commit()
                continue
            org = session.scalars(select(Org).where(Org.id == endpoint.org_id)).one()
            report.queued += advance(session, endpoint, org, now())
            claimed_at = now()
            claimed = _claim(session, endpoint.id, claimed_at)
            session.commit()
            org_id = endpoint.org_id
            sending = time.monotonic()
            deliver_claimed(session, endpoint, claimed, report, claimed_until=claimed_at + CLAIM_LEASE, now=now,
                            deadline=_turn(deadline, spent, org_id))
            spent[org_id] = spent.get(org_id, 0.0) + time.monotonic() - sending
        except Exception:  # noqa: BLE001 - one endpoint must not stall the rest; retried next pass
            session.rollback()
            log.exception("webhooks: endpoint %s pass failed", endpoint_id)


def run(now: Optional[datetime] = None) -> Report:
    """The `webhooks` cron: one pass on the operator plane (`every_org_session`)."""
    from app.db import every_org_session

    report = Report()
    started = time.monotonic()
    clock = (lambda: now) if now is not None else utcnow
    deadline = lambda: time.monotonic() - started > PASS_BUDGET_SECONDS  # noqa: E731
    with every_org_session() as session:
        pass_once(session, report, now=clock, deadline=deadline)
    return report


# ── The PR events (`pr:opened`, `pr:merged`) ──────────────────────────────────────────────────────────


def record_pr_event(session: Session, task: Any, kind: str, *, pr_url: Optional[str], pr_number: Optional[int],
                    merge_sha: Optional[str] = None) -> None:
    """One `activity_events` row for a task's PR opening or merging — the source a `pr.*` webhook is made from,
    and a line on the org's Audit page — in the caller's transaction; then the nudge. Once per task, kind and PR: a
    merge reported again (the lane, a self-merge attestation, the ledger's reconcile) records nothing new."""
    seen = session.scalar(select(ActivityEvent.id).where(
        ActivityEvent.org_id == task.org_id, ActivityEvent.kind == kind,
        ActivityEvent.data["task_id"].astext == str(task.id),
        ActivityEvent.data["pr_url"].astext == (pr_url or ""),
    ).limit(1))
    if seen is not None:
        return
    verb = "Opened" if kind == "pr:opened" else "Merged"
    data: dict[str, Any] = {"task_id": task.id, "pr_url": pr_url or "", "pr_number": pr_number}
    if merge_sha:
        data["merge_sha"] = merge_sha
    session.add(ActivityEvent(
        org_id=task.org_id, action_type="write", kind=kind,
        summary=f"{verb} pull request" + (f" #{pr_number}" if pr_number is not None else "") + f" for task {task.id}",
        data=data,
    ))
    nudge_after_commit(session)
