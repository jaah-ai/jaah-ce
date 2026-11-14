"""A tracker's inbound request → what it means for Jaah (`routes/trackers.py` is the door).

    connection → adapter.verify → adapter.parse → per event: start, follow-up, or stop

Every event lands as one delivery of the connection's trigger, so Automations shows it in the automation's runs:
- **start** (`assigned`, `mentioned`, `labeled`) on an issue with no live task is a `received` delivery the
  trigger processor files through the default automation (`trigger_intake`, `automation_fire`); its dedup key is the
  event's (`<tracker>:<site>:<issue>:<event id>`), so a redelivered event files nothing. The filed task's
  `tracker_issue_links` row is written in the task's own commit (`link_filed_task`, called by
  `automation_fire.file_task_target`) and the Target is narrowed to the issue's mapped project (`narrowed`);
- **follow-up** — a mention on an issue with a live task is a reply to it (below: an answer or a comment on the task,
  only from someone who may act on it); any other start on it is a duplicate. One issue has at most one live task in
  the org however many connections hear it: two connections of one site (a Jira API-token one and the Forge app,
  mapping the same project) share the dedup (`site_live_link`). Both may receive the same start; the first to file
  wins and the other's target is skipped (`link_filed_task`, serialized per issue by `lock_issue`), so a start that
  fails to file never swallows the other connection's. A mention that arrives while its issue's first start is still waiting to
  file is received like a start and, at filing, becomes a comment on the task that start filed (`follow_up_task`,
  committed with the target's ledger row), never a lost event. The comment is authored by the system actor, never by
  a member: a tracker user is not a Jaah session, so nothing they write may read as a member's words. The matched
  member, if any, is kept on the delivery's preview (`member_id`);
- **stop** (`unassigned`, `unlabeled`) stops the issue's live task the tracker started (`origin` `intake`), never one
  linked by hand, `stop_reason` "stopped from <tracker>";
- **reply** — a `comment` (or a mention) on an issue with a live task answers its open question or follows it up
  (`trackers.replies`); what is said back to the issue is the write-back's (`trackers.writeback`).

The door, in order (`ingest`): 413 over the body cap; an unknown connection is 404 on a token URL but 401 on the
shared route, word for word a failed `verify`, because a workspace id is public and must not tell a caller which
workspaces use Jaah; 429 over the unverified budget, one per client address and the connection the caller CLAIMS
(the workspace id it names, or its URL token's hash), counted before that claim is looked up so a spent budget never
tells a known id from an unknown one; 401 a refused `verify`; only then 429 over the connection's own budget, which
only verified events spend. A refused `verify` is logged as a
`skipped` delivery at most once per trigger per minute, so a flood cannot bury the events list; nothing else happens.

Accepted limits: an unknown id answers without the connection's lookup and `verify`, so a caller averaging many
timings may still tell the two apart; and a forger sending from the real tracker's own addresses (a shared egress,
such as Atlassian's for every Forge app) shares its unverified budget, so that budget is three times the verified one.
There is deliberately no budget per address across claims: on a shared egress it would let one tenant's flood 429
every other tenant's events.

Attribution: the tracker user is the org member with the same email, matched only when the adapter says the tracker
verified it (`Actor.email_verified`). For a start, the tracker account the connection itself signs in as (the
adapter's `own_account`: Jira's account id, Backlog's user) is also the member who connected it (`starter_for`):
Jira Cloud hides account emails and Backlog verifies none, so a person who connected with their own token would
otherwise read as a stranger. The trust boundary this accepts: whoever can act as that account in the tracker (its
password, a Jira Automation rule run as it) starts tasks as the connecting member, as does a new owner who takes the
automation over. A user matched neither way may start a task or add a follow-up only while the
connection's `allow_unmapped_users` is on (default off). A stop is honoured from anyone the tracker lets change the
issue: it only ever halts work. The task's link records who started it (`started_by_external`).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.db import BridgedSession
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.identity import AdminUser
from app.models.tasks import Task
from app.models.trackers import TrackerConnection, TrackerIssueLink, TrackerWriteback
from app.ratelimit import SharedRateLimiter
from app.task_lifecycle import DONE, PAUSED, SYSTEM_FAILED
from app.task_sections import TRACKER_STOP_REASON, TRACKER_STOP_REASONS
from app.tenancy import seam
from app.trackers import connections, project_links, protocol
from app.trackers.protocol import Actor, IssueEvent
from app.trackers.vocabulary import (
    KIND_COMMENT,
    KIND_MENTIONED,
    ORIGIN_INTAKE,
    START_KINDS,
    STOP_KINDS,
    TRACKER_LABELS,
    TRACKERS,
    WRITEBACK_ACK,
)
from app.triggers import DEDUPED, FILTERED, MAX_BODY_BYTES, RECEIVED, SKIPPED, STOPPED_TASK_REASON

log = logging.getLogger("tracker-intake")

# A task in one of these no longer works on its issue: the next start files a new one. A task the TRACKER paused
# (`TRACKER_STOP_REASONS`) has ended too, so starting the issue again starts work again; any other pause (an
# operator's Stop, a balance pause) keeps it the issue's live task.
_ENDED_STATES = (DONE, SYSTEM_FAILED)

REASON_BAD_SIGNATURE = "invalid signature"
REASON_UNREADABLE = "unreadable event"
REASON_NO_TRIGGER = "the connection's automation was deleted — reconnect"

# Shared across replicas. Verified events only, per connection: a tracker's burst never fills the deliveries table.
_LIMITER = SharedRateLimiter(limit=300, window=60.0, bucket="tracker-ingress")
# Every request before `verify`, per claimed connection and client address: a forger naming a public workspace id
# spends its own budget, never the real tracker's (which posts from its own addresses).
_UNVERIFIED_LIMITER = SharedRateLimiter(limit=900, window=60.0, bucket="tracker-ingress-unverified")
# One `invalid signature` delivery per trigger per minute: a forged flood is logged, not written row by row.
_BAD_SIGNATURE_LOG = SharedRateLimiter(limit=1, window=60.0, bucket="tracker-ingress-bad-signature")
_NO_CONNECTION_KEY = "-"


@dataclass(frozen=True)
class IntakeAnswer:
    status: int
    body: dict[str, Any]
    # (connection id, the event) for each start an `ack_sync` adapter must acknowledge before the response.
    acks: list[tuple[UUID, IssueEvent]] = field(default_factory=list)
    # What the route does for each reply after the commit: an answer to give, or the one refusal to post.
    replies: list[Any] = field(default_factory=list)


# ── Finding the connection (a public route: nothing is bound yet) ─────────────────────────────────────


@contextmanager
def connection_session(connection_id: UUID) -> Iterator[tuple[Session, Any]]:
    """A session bound to the connection's own org: every write about an event lands there."""
    with BridgedSession() as session, seam.detached(), seam.bind_from(session, TrackerConnection, connection_id) as org:
        yield session, org


@dataclass(frozen=True)
class Claim:
    """The connection a request names, before anyone looks it up: a URL token's hash, or the workspace id."""
    column: str
    value: str


def claimed_connection(tracker: str, connection_token: Optional[str], headers: Mapping[str, str],
                       raw: bytes) -> Optional[Claim]:
    """What the request names: its URL token (hashed), else the workspace or site id the adapter reads from the
    payload (`connection_key`). None when it names nothing usable."""
    adapter = protocol.adapter_for(tracker)
    if tracker not in TRACKERS or adapter is None:
        return None
    if connection_token is not None:
        if not connection_token or len(connection_token) > 128:
            return None
        return Claim("connection_token_hash", connections.token_hash(connection_token))
    key_of = getattr(adapter, "connection_key", None)
    try:
        key = key_of(headers, raw) if key_of is not None else None
    except Exception:  # noqa: BLE001 — a payload the adapter cannot read names no workspace
        key = None
    if not key or len(str(key)) > 255:
        return None
    return Claim("external_org_id", str(key))


def _binding_claim(tracker: str, headers: Mapping[str, str], raw: bytes) -> Optional[Claim]:
    """The claim code an unbound site's call carries (hashed, `binding_key`), naming a connection no site holds yet.
    None for an adapter with no such code."""
    key_of = getattr(protocol.adapter_for(tracker), "binding_key", None)
    try:
        key = key_of(headers, raw) if key_of is not None else None
    except Exception:  # noqa: BLE001 — a payload the adapter cannot read names no connection
        key = None
    return Claim("claim_code_hash", key) if key else None


def resolve_connection(tracker: str, claim: Optional[Claim]) -> Optional[UUID]:
    """The connection `claim` names, or None when there is none."""
    if claim is None:
        return None
    where = getattr(TrackerConnection, claim.column) == claim.value
    from app.trigger_intake import fleet_session

    with fleet_session() as session:
        return session.scalar(
            select(TrackerConnection.id).where(where, TrackerConnection.tracker == tracker)
            .order_by(TrackerConnection.created_at).limit(1)
        )


# ── The request ───────────────────────────────────────────────────────────────────────────────────────


_NOT_FOUND = IntakeAnswer(404, {"detail": "Not found."})
_UNVERIFIED = IntakeAnswer(401, {"detail": "Invalid signature."})
_TOO_MANY = IntakeAnswer(429, {"detail": "Too many requests."})


def ingest(tracker: str, connection_token: Optional[str], raw: Optional[bytes], headers: Mapping[str, str],
           query: Mapping[str, str], *, client_ip: str = "unknown") -> IntakeAnswer:
    """The ingress's whole effect, in the order the module docstring gives, ending 202 with what each event came to."""
    if raw is None or len(raw) > MAX_BODY_BYTES:
        return IntakeAnswer(413, {"detail": "The body is over 1 MiB."})
    claim = claimed_connection(tracker, connection_token, headers, raw)
    # The shared route's unknown workspace is a failed signature; a token URL's unknown token stays 404.
    missing = _NOT_FOUND if connection_token is not None else _UNVERIFIED
    # Charged by what the caller CLAIMS, before the lookup: a known and an unknown id spend alike.
    scope = f"{tracker}:{claim.column}:{claim.value}" if claim is not None else f"{tracker}:{_NO_CONNECTION_KEY}"
    if not _UNVERIFIED_LIMITER.allow(f"{scope}:{client_ip}"):
        return _TOO_MANY
    connection_id = resolve_connection(tracker, claim)
    if connection_id is None and connection_token is None:
        # An unbound site's first call (a Jira Forge app) names its connection by the claim code it carries.
        connection_id = resolve_connection(tracker, _binding_claim(tracker, headers, raw))
    if connection_id is None:
        log.warning("tracker %s: refused a call naming no connection (%s)", tracker,
                    claim.column if claim is not None else "nothing")
        return missing
    adapter = protocol.adapter_for(tracker)
    assert adapter is not None  # resolve_connection found one
    try:
        return _ingest_bound(tracker, connection_id, adapter, raw, headers, query)
    except seam.TenancyRowMissing:
        # Disconnected between the lookup and here: as if it had never been found.
        return missing


def _verified(adapter, conn: TrackerConnection, headers: Mapping[str, str], raw: bytes,
              query: Mapping[str, str]) -> bool:
    """The adapter's verdict; an adapter that raises on a malformed request has refused it."""
    try:
        return bool(adapter.verify(conn, headers, raw, query))
    except Exception:  # noqa: BLE001 — a request the adapter cannot even check is an unverified one
        log.warning("tracker %s connection %s: verify raised; refused", conn.tracker, conn.id, exc_info=True)
        return False


def _ingest_bound(tracker: str, connection_id: UUID, adapter, raw: bytes, headers: Mapping[str, str],
                  query: Mapping[str, str]) -> IntakeAnswer:
    received = False
    acks: list[tuple[UUID, IssueEvent]] = []
    replies: list[Any] = []
    with connection_session(connection_id) as (session, org):
        conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        if conn is None:
            raise seam.TenancyRowMissing("tracker connection")
        trigger = connections.trigger_of(session, conn)
        # Verified before anything else is said: an unauthenticated caller learns nothing about the connection.
        if not _verified(adapter, conn, headers, raw, query):
            if trigger is not None and _BAD_SIGNATURE_LOG.allow(str(trigger.id)):
                _log(session, trigger, SKIPPED, REASON_BAD_SIGNATURE)
                session.commit()
            log.warning("tracker %s connection %s: refused an event that failed verification", tracker, conn.id)
            return _UNVERIFIED
        # Charged only now: an unverified request never spends the connection's own budget.
        if not _LIMITER.allow(str(conn.id)):
            return _TOO_MANY
        if trigger is None:
            return IntakeAnswer(410, {"detail": REASON_NO_TRIGGER})
        try:
            events = adapter.parse(conn, headers, raw)
        except Exception:  # noqa: BLE001 — a body the adapter cannot read is the sender's fault, logged
            log.exception("tracker %s connection %s: could not parse an event", tracker, conn.id)
            _log(session, trigger, SKIPPED, REASON_UNREADABLE)
            session.commit()
            return IntakeAnswer(400, {"detail": "Unreadable event."})
        # What `parse` learned about the connection (a Forge site bound, its fresh token) lands even with no event.
        try:
            session.commit()
        except IntegrityError:
            # Two connections binding one site at once: the unique index let one through; this one is refused.
            session.rollback()
            log.warning("tracker %s connection %s: its site is another connection's; refused", tracker, connection_id)
            return _UNVERIFIED
        from app import trigger_health

        stopped = trigger_health.stopped_reason(trigger)
        outcomes: list[dict[str, Any]] = []
        for event in events:
            row: Optional[TriggerDelivery]
            if stopped is not None:
                row = _log(session, trigger, SKIPPED, stopped, preview=_preview(event))
            else:
                row = _handle(session, org, conn, trigger, event, replies)
            session.commit()
            if row is None:
                continue
            outcomes.append({"delivery_id": row.id, "state": row.state, "reason": row.reason})
            if row.state == RECEIVED:
                received = True
                if adapter.ack_sync:
                    acks.append((conn.id, event))
        trigger_id = trigger.id
    if received:
        from app import trigger_intake

        if acks:
            # The tracker waits on the acknowledgement: file now, in this request, so `ack` can name the task.
            trigger_intake.process_trigger(trigger_id)
        else:
            trigger_intake.fire_intake()
    return IntakeAnswer(202, {"deliveries": outcomes}, acks, replies)


def _handle(session: Session, org: UUID, conn: TrackerConnection, trigger: TaskTrigger,
            event: IssueEvent, replies: list[Any]) -> Optional[TriggerDelivery]:
    """One event's delivery row (staged; the caller commits), or None for an event that is not for Jaah. A reply
    appends the route's after-commit work to `replies`."""
    preview = _preview(event)
    from app.trigger_intake import dedup_key_for

    key = dedup_key_for(f"{conn.tracker}:{event.issue.site}:{event.issue.key}:{event.event_id}")
    if event.kind == KIND_COMMENT or (event.kind == KIND_MENTIONED and (event.text or "").strip()
                                      and live_link(session, conn.id, event.issue.key) is not None):
        # A comment, or a mention, on an issue whose task is at work: a reply to that task, held to who may act on
        # it (`trackers.replies`), never a start's membership test.
        from app.trackers import replies as tracker_replies

        return tracker_replies.handle(session, org, conn, trigger, event, key, replies)
    if event.kind not in STOP_KINDS and event.kind not in START_KINDS:
        return None
    first = session.scalar(select(TriggerDelivery.id).where(
        TriggerDelivery.trigger_id == trigger.id, TriggerDelivery.dedup_key == key).limit(1))
    if first is not None:
        # A redelivery (a tracker retrying a slow answer): what the first did is not done twice.
        return _log(session, trigger, DEDUPED, f"duplicate of delivery #{first}", preview=preview)
    if event.kind in STOP_KINDS:
        return _stop(session, conn, trigger, event, preview, key)
    settings = connections.settings_of(conn)
    member = starter_for(session, org, conn, trigger, event.actor)
    # Who the tracker user is in Jaah, kept beside the event (a follow-up is authored by the system actor).
    preview["tracker"]["member_id"] = str(member.id) if member is not None else None
    if member is None and not settings["allow_unmapped_users"]:
        return _log(session, trigger, FILTERED,
                    f"{event.actor.name or event.actor.external_id} is not a member of this organization",
                    preview=preview)
    live = site_live_link(session, conn, event.issue.key)
    if live is not None:
        return _log(session, trigger, DEDUPED, f"Task #{live.task_id} already works on {event.issue.key}",
                    preview=preview, task_id=live.task_id, dedup_key=key)
    project_id = project_links.primary_project_for(session, conn.id, event.issue.container_id)
    if project_id is None:
        return _log(session, trigger, FILTERED, f"no Jaah project is mapped to {event.issue.container_id}",
                    preview=preview, dedup_key=key)
    tracker_payload = tracker_event(conn, event, project_id, member)
    return _log(session, trigger, RECEIVED, None, preview=preview, event={"tracker": tracker_payload},
                dedup_key=key)


def _log(session: Session, trigger: TaskTrigger, state: str, reason: Optional[str], *,
         preview: Optional[dict[str, Any]] = None, event: Optional[dict[str, Any]] = None,
         dedup_key: Optional[str] = None, task_id: Optional[int] = None) -> TriggerDelivery:
    """Stage one delivery of the connection's trigger, flushed (its id read). A `received` one keeps its event for the
    processor; every other is settled here and keeps the preview only."""
    from app.trigger_intake import preview_of

    row = TriggerDelivery(
        trigger_id=trigger.id, org_id=trigger.org_id, state=state, reason=reason, event=event,
        preview=preview_of(preview) if preview is not None else None, dedup_key=dedup_key, task_id=task_id,
        processed_at=None if state == RECEIVED else utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _preview(event: IssueEvent) -> dict[str, Any]:
    return {"tracker": {
        "kind": event.kind, "issue_key": event.issue.key, "issue_url": event.issue.url,
        "container_id": event.issue.container_id, "actor": event.actor.name or event.actor.external_id,
        "title": event.title, "text": event.text,
    }}


def tracker_event(conn: TrackerConnection, event: IssueEvent, project_id: int,
                  member: Optional[AdminUser]) -> dict[str, Any]:
    """The `{{tracker.*}}` namespace a start's task renders from (`connections.BODY_TEMPLATE`), plus what the link
    needs (`link_filed_task`). `prompt` is the task's body: a mention's comment first, as the instruction, then the
    issue (title, body — the adapter's `body` carries its recent comments) and its link."""
    instruction = (event.text or "").strip() if event.kind == KIND_MENTIONED else ""
    issue = f"# {event.issue.key}: {event.title}".rstrip()
    if event.body.strip():
        issue += f"\n\n{event.body.strip()}"
    issue += f"\n\n{TRACKER_LABELS[conn.tracker]} issue: {event.issue.url}"
    prompt = (f"{instruction}\n\n---\n\n{issue}" if instruction else issue).strip()
    return {
        "connection_id": str(conn.id), "tracker": conn.tracker, "site": event.issue.site,
        "issue_key": event.issue.key, "issue_url": event.issue.url, "container_id": event.issue.container_id,
        "event_id": event.event_id, "kind": event.kind, "title": " ".join(event.title.split()) or event.issue.key,
        "body": event.body, "text": event.text, "comment_id": event.comment_id, "session_id": event.session_id,
        "prompt": prompt, "project_id": project_id,
        "actor": {"external_id": event.actor.external_id, "name": event.actor.name, "email": event.actor.email},
        "member_id": str(member.id) if member is not None else None,
    }


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────


def member_for(session: Session, org: UUID, actor: Actor) -> Optional[AdminUser]:
    """The enabled, non-system member of `org` whose email is the actor's (case-insensitive), or None — always None
    for an email the tracker has not verified (`Actor.email_verified`)."""
    email = actor.email
    if not actor.email_verified or not email or not email.strip():
        return None
    return session.scalars(select(AdminUser).where(
        func.lower(AdminUser.email) == email.strip().lower(), AdminUser.org_id == org,
        AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None),
    ).order_by(AdminUser.created_at)).first()


def starter_for(session: Session, org: UUID, conn: TrackerConnection, trigger: TaskTrigger,
                actor: Actor) -> Optional[AdminUser]:
    """Who a start is from: the member `member_for` matches, else — when the actor is the tracker account the
    connection signs in as (the adapter's `own_account`) — the enabled member of `org` who connected it (the
    connection trigger's owner). An account that holds the connection's own token is that member's."""
    member = member_for(session, org, actor)
    if member is not None or not actor.external_id:
        return member
    own_of = getattr(protocol.adapter_for(conn.tracker), "own_account", None)
    try:
        own = own_of(conn) if own_of is not None else None
    except Exception:  # noqa: BLE001 — a tracker that cannot say who the connection is matches nobody
        log.warning("tracker %s connection %s: could not read its own account", conn.tracker, conn.id, exc_info=True)
        own = None
    if not own or str(own) != actor.external_id:
        return None
    from app.trigger_intake import trigger_owner

    owner = trigger_owner(session, trigger)
    if owner is None or owner.org_id != org or owner.disabled_at is not None:
        return None
    return owner


def live_clause() -> Any:
    """A task still at work on its issue: any state but done or failed, not archived, not paused by the tracker."""
    return and_(
        Task.state.not_in(_ENDED_STATES), Task.archived_at.is_(None),
        or_(Task.state != PAUSED, Task.stop_reason.is_(None), Task.stop_reason.not_in(TRACKER_STOP_REASONS)),
    )


def live_link(session: Session, connection_id: UUID, issue_key: str,
              origin: Optional[str] = None) -> Optional[TrackerIssueLink]:
    """The link of the issue's newest task still at work (`live_clause`) through this connection, of any origin
    unless `origin` names one (`LINK_ORIGINS`): a stop reaches only what the tracker started, a reply any linked task.
    A start's dedup looks across the site (`site_live_link`)."""
    return _live_link_in(session, [connection_id], issue_key, origin)


def site_live_link(session: Session, conn: TrackerConnection, issue_key: str) -> Optional[TrackerIssueLink]:
    """`live_link` through any of `site_connection_ids`: one issue starts at most one task in the org, however many
    of its connections hear the start."""
    return _live_link_in(session, site_connection_ids(session, conn), issue_key, None)


def _live_link_in(session: Session, connection_ids: list[UUID], issue_key: str,
                  origin: Optional[str]) -> Optional[TrackerIssueLink]:
    query = select(TrackerIssueLink).join(Task, Task.id == TrackerIssueLink.task_id).where(
        TrackerIssueLink.connection_id.in_(connection_ids), TrackerIssueLink.issue_key == issue_key, live_clause(),
    )
    if origin is not None:
        query = query.where(TrackerIssueLink.origin == origin)
    return session.scalars(query.order_by(TrackerIssueLink.id.desc())).first()


def site_connection_ids(session: Session, conn: TrackerConnection) -> list[UUID]:
    """The org's connections to `conn`'s tracker site, itself included: two Jira connections of one site (an API-token
    one and the Forge app) both hear every event on it. `conn` alone when it names no site."""
    site = _site_key(conn.site)
    if not site:
        return [conn.id]
    found = [row.id for row in session.execute(select(TrackerConnection.id, TrackerConnection.site).where(
        TrackerConnection.org_id == conn.org_id, TrackerConnection.tracker == conn.tracker,
        TrackerConnection.site.is_not(None),
    )) if _site_key(row.site) == site]
    return found if conn.id in found else [conn.id, *found]


def _site_key(site: Optional[str]) -> str:
    return (site or "").strip().lower()


def lock_issue(session: Session, conn: TrackerConnection, issue_key: str) -> None:
    """Serialize filing one issue across the site's connections until the caller's commit: both connections may
    receive one label, and the second to file must see the first's link."""
    lock = f"tracker-issue:{conn.org_id}:{conn.tracker}:{_site_key(conn.site) or conn.id}:{issue_key}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock, 0))))


def jaah_accounts(session: Session, conn: TrackerConnection) -> set[str]:
    """The tracker accounts Jaah writes as on `conn`'s site, one per connection that keeps it on its settings (the
    adapter's `account_setting`): a comment by any of them may be Jaah's own write-back, never a reply to refuse."""
    adapter = protocol.adapter_for(conn.tracker)
    setting = getattr(adapter, "account_setting", None)
    if not setting:
        return set()
    ids = site_connection_ids(session, conn)
    rows = session.scalars(select(TrackerConnection.settings).where(TrackerConnection.id.in_(ids))).all()
    return {str(row[setting]) for row in rows if isinstance(row, dict) and row.get(setting)}


def _acting_user(session: Session, trigger: TaskTrigger, member: Optional[AdminUser]) -> Optional[AdminUser]:
    """Who a stop is recorded as: the matched member, else the connection trigger's owner (the admin who connected
    the tracker)."""
    if member is not None:
        return member
    from app.trigger_intake import trigger_owner

    return trigger_owner(session, trigger)


# ── Follow-up and stop ────────────────────────────────────────────────────────────────────────────────


def followed_up_reason(task_id: int) -> str:
    return f"added to Task #{task_id} as a comment"


def _system_author(session: Session) -> AdminUser:
    """The system actor's row (`entity_shape.SYSTEM_ACTOR_ID`): a tracker's words are the tracker's, not a member's."""
    with seam.identity_scope():
        author = session.scalar(select(AdminUser).where(AdminUser.id == SYSTEM_ACTOR_ID))
    if author is None:  # pragma: no cover — the row is seeded by migration and never deleted
        raise RuntimeError("the system actor row is missing")
    return author


def _follow_up(session: Session, tracker: str, task_id: int, issue_key: str, who: str, text: str) -> None:
    """Stage the comment, authored by the system actor and naming the tracker user it came from."""
    from app import comment_service

    comment_service.create(
        session, comment_service.Subject("task", task_id), author=_system_author(session),
        body=f"{who} in {TRACKER_LABELS[tracker]} ({issue_key}):\n\n{text.strip()}",
        mentioned=lambda _comment: [],
    )


def _stop(session: Session, conn: TrackerConnection, trigger: TaskTrigger, event: IssueEvent,
          preview: dict[str, Any], key: str) -> TriggerDelivery:
    from app.actors import set_request_actor
    from app.routes.tasks.transitions import _stop_task_core

    stop_reason = TRACKER_STOP_REASON.format(tracker=conn.tracker)
    withdrawn = _withdraw_pending_starts(session, trigger, event.issue.key, stop_reason)
    # Only what the tracker started: a task linked to the issue by hand is a person's, and keeps running.
    live = live_link(session, conn.id, event.issue.key, origin=ORIGIN_INTAKE)
    if live is None:
        reason = (f"withdrew {withdrawn} start(s) not yet filed" if withdrawn
                  else f"no task is working on {event.issue.key}")
        return _log(session, trigger, FILTERED, reason, preview=preview, dedup_key=key)
    task_id = live.task_id
    member = member_for(session, trigger.org_id, event.actor)
    user = _acting_user(session, trigger, member)
    if user is None:
        return _log(session, trigger, FILTERED, "no member to record the stop as", preview=preview, task_id=task_id,
                    dedup_key=key)
    trigger_id = trigger.id
    set_request_actor(session, user.id)
    try:
        # Commits on success (and rolls back a no-op): the delivery row is written after it, in a fresh transaction.
        landed = _stop_task_core(task_id, session, user, stop_reason=stop_reason)
    finally:
        set_request_actor(session, None)
    trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id))
    reason = STOPPED_TASK_REASON.format(task_id=task_id) if landed else f"Task #{task_id} was not running"
    return _log(session, trigger, FILTERED, reason, preview=preview, task_id=task_id, dedup_key=key)


def _withdraw_pending_starts(session: Session, trigger: TaskTrigger, issue_key: str, reason: str) -> int:
    """Settle the issue's starts still waiting for the processor (`received`): a stop that arrives before its start
    files must win, or the task would run on an issue nobody wants worked on. Returns how many. A row the processor
    holds right now is skipped (`SKIP LOCKED`): it files, and its task is the live one the stop then finds."""
    rows = session.scalars(select(TriggerDelivery).where(
        TriggerDelivery.trigger_id == trigger.id, TriggerDelivery.state == RECEIVED,
        TriggerDelivery.event["tracker"]["issue_key"].astext == issue_key,
    ).with_for_update(skip_locked=True)).all()
    now = utcnow()
    for row in rows:
        row.state, row.reason, row.event, row.processed_at = FILTERED, f"{reason} before it was filed", None, now
    session.flush()
    return len(rows)


# ── Seams the fire engine calls (`automation_fire`) ───────────────────────────────────────────────────


def narrowed(resolution, event: Optional[Mapping[str, Any]]):
    """`resolution` cut to the project the event's issue is mapped to: a tracker start files one task, in its issue's
    project, never one per mapped project. Only ever narrows the automation's own Target (AC-2.4)."""
    from app.automations import TARGET_PROJECTS
    from app.target_resolver import Resolution

    payload = (event or {}).get("tracker") if isinstance(event, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("project_id") is None:
        # No issue (a Run now's pasted sample without one): nothing to file, never a blank task per project.
        return Resolution(dangling=list(resolution.dangling))
    wanted = str(int(payload["project_id"]))

    def keep(item) -> bool:
        return item.kind == TARGET_PROJECTS and str(item.id) == wanted

    return Resolution(
        fire=[item for item in resolution.fire if keep(item)],
        skipped=[(item, reason) for item, reason in resolution.skipped if keep(item)],
        dangling=list(resolution.dangling),
    )


def follow_up_task(session: Session, event: Optional[Mapping[str, Any]]) -> Optional[int]:
    """For a mention received while its issue had no live task yet (its first start was still waiting to file): the
    task that start has filed by now, which the mention follows up instead of filing. None to file as a start. Reads
    only."""
    payload = (event or {}).get("tracker") if isinstance(event, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("kind") != KIND_MENTIONED or not payload.get("connection_id"):
        return None
    if not str(payload.get("text") or "").strip():
        return None
    live = live_link(session, UUID(str(payload["connection_id"])), str(payload["issue_key"]))
    return live.task_id if live is not None else None


def stage_follow_up(session: Session, event: Mapping[str, Any], task_id: int) -> None:
    """Stage `follow_up_task`'s comment (the caller commits it with the target's ledger row)."""
    payload = event["tracker"]
    actor = payload.get("actor") or {}
    _follow_up(session, str(payload.get("tracker") or ""), task_id, str(payload["issue_key"]),
               str(actor.get("name") or actor.get("external_id") or ""), str(payload.get("text") or ""))


def link_filed_task(session: Session, task: Task, event: Optional[Mapping[str, Any]]) -> None:
    """Stage the filed task's `tracker_issue_links` row, in the task's own commit. Refuses (the target is skipped,
    nothing filed) when the issue already has a live task through any connection of its site — the per-issue rule,
    held here too because two different starts of one issue can both be received before either files."""
    from app.automation_fire import TargetFailed
    from app.automations import OUTCOME_SKIPPED

    payload = (event or {}).get("tracker") if isinstance(event, Mapping) else None
    if not isinstance(payload, Mapping) or not payload.get("connection_id"):
        return
    connection_id = UUID(str(payload["connection_id"]))
    conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
    if conn is not None:
        lock_issue(session, conn, str(payload["issue_key"]))
        live = site_live_link(session, conn, str(payload["issue_key"]))
    else:
        live = live_link(session, connection_id, str(payload["issue_key"]))
    if live is not None and live.task_id != task.id:
        raise TargetFailed(f"Task #{live.task_id} already works on {payload['issue_key']}", OUTCOME_SKIPPED)
    actor = payload.get("actor") or {}
    session.add(TrackerIssueLink(
        org_id=task.org_id, task_id=task.id, connection_id=connection_id,
        issue_key=str(payload["issue_key"]), issue_url=str(payload.get("issue_url") or ""),
        container_id=str(payload.get("container_id") or ""), session_id=payload.get("session_id"),
        origin=ORIGIN_INTAKE,
        started_by_external={
            "tracker": payload.get("tracker"), "user_id": actor.get("external_id"), "name": actor.get("name"),
            "member_id": payload.get("member_id"),
        },
    ))
    session.flush()


# ── The synchronous acknowledgement (`ack_sync` adapters) ──────────────────────────────────────────────


@dataclass(frozen=True)
class AckWork:
    connection: TrackerConnection
    task_id: int
    issue: protocol.IssueRef
    session_id: Optional[str]


def pending_ack(connection_id: UUID, event: IssueEvent) -> Optional[AckWork]:
    """The filed task to acknowledge for `event`, unless it has none yet or was already acknowledged."""
    with connection_session(connection_id) as (session, _org):
        conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        link = live_link(session, connection_id, event.issue.key)
        if conn is None or link is None:
            return None
        done = session.scalar(select(TrackerWriteback.id).where(
            TrackerWriteback.task_id == link.task_id, TrackerWriteback.kind == WRITEBACK_ACK))
        if done is not None:
            return None
        session.expunge(conn)
        return AckWork(conn, link.task_id, event.issue, event.session_id)


def record_ack(connection_id: UUID, task_id: int) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with connection_session(connection_id) as (session, org):
        session.execute(pg_insert(TrackerWriteback).values(
            org_id=org, task_id=task_id, kind=WRITEBACK_ACK, ref="",
        ).on_conflict_do_nothing(constraint="uq_tracker_writebacks_task_kind_ref"))
        session.commit()
