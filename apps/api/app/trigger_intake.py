"""Trigger intake — ingest an event as one `trigger_deliveries` row, then process rows into filed Tasks.

**Ingest fast, process durably, file through the existing seam.** A source turns its event into one `received`
row and returns at once (`ingest_http` for the webhook route). The `trigger-intake` system activity is the ONLY
processor: per trigger, under that trigger's org, it walks one fixed pipeline per row —

    health gate → owner authority → preflight → dedup → rate → fire

— and files through the fire engine (`app/automation_fire.py`), one task per target of the trigger's automation:
the concurrency cap and queue are per target there (a target at its cap is owed on the delivery, which stays
`queued` until every owed target files), the template renders once per target, and each target commits on its own
under the delivery's `filing` claim. A trigger with one project behaves exactly as it always did. An ingest and a
closing filed Task fire the activity early (`cron_runtime.fire_now_activity_sync`, `task_wake`); its 60 s
interval is the backstop, because the wake queue drops on overflow.

Exactly-once per key: the dedup reservation (`trigger_dedup`, `INSERT … ON CONFLICT DO NOTHING`) is committed with
the delivery's claim, and survives only for a delivery that filed at least one target, or is `queued`. Each trigger
is processed under a `FOR UPDATE SKIP LOCKED` lock on its own row, and a pass leaves a trigger alone while another
holds a live `filing` claim on one of its deliveries, so two replicas never interleave one trigger's counters, limits
or queue. A `filing` claim that expired (a crashed replica) is resumed for the targets with no ledger row only.

Preflight (§3.6) is a registry (`PREFLIGHT_RUNNERS`); this module registers `none`, and the venue modules
imported at the bottom register theirs (`trigger_preflight_script` `script`, `trigger_preflight_model` `model`). A
preflighted row is claimed into `preflighting` (at most one per trigger, fleet-wide, by a partial unique index), the
transaction commits, and the call runs on `PREFLIGHT_POOL` — the pass never waits on it. The worker applies its
result only while its claim still stands (`apply_preflight_result`, a compare-and-swap), then fires the activity
early.

The secret never enters a row, an event, a preflight input or a filed body: `ingest_http` verifies it and drops
every credential header before the event is stored (`redact_headers`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import edition_features, trigger_health, trigger_secrets
from app.operator_settings import reads as settings_reads
from app.db import SessionLocal
from app.entity_shape import readable_error
from app.models.automations import AutomationRunTarget, TaskTrigger, TriggerDedup, TriggerDelivery
from app.models.identity import AdminUser
from app.models.notifications import InboundEmail
from app.models.projects import Project
from app.models.tasks import Task
from app.one_line import one_line
from app.ratelimit import SharedRateLimiter
from app.inbound_sender import DAILY_ROUTED_LIMIT, aligned, from_domain
from app.automations import MODE_SPECIFIC, TARGET_PROJECTS
from app.task_lifecycle import ACTIVE, _EPIC_SETTLED_STATES
from app.tenancy import seam
from app.tenancy.ids import ORG_ZERO_ID
from app.trigger_template import render, render_title
from app.triggers import (
    AUTH_API_KEY,
    AUTH_GITHUB,
    DEDUPED,
    DELIVERY_ID_HEADER,
    DISPATCHED,
    FAILED,
    FILING,
    FILTERED,
    GITHUB_DELIVERY_HEADER,
    GITHUB_EVENT_HEADER,
    HTTP_SOURCE_KINDS,
    INTAKE_ACTIVITY,
    MAX_BODY_BYTES,
    MAX_DEDUP_KEY_CHARS,
    MAX_PREFLIGHT_OUTPUT_BYTES,
    MAX_PREVIEW_BYTES,
    MAX_STATE_BYTES,
    PREFLIGHT_NONE,
    PREFLIGHTING,
    QUEUED,
    RATE_CAPPED,
    RECEIVED,
    SKIPPED,
    SOURCE_GITHUB,
    SOURCE_GMAIL,
    SOURCE_SLACK,
    SOURCE_USER_REGISTERED,
    UNMATCHED,
    USER_MOMENT_APPLIED,
    USER_SCOPE_ORG,
    USER_SCOPE_PLATFORM,
)

if TYPE_CHECKING:
    from app import api_keys

log = logging.getLogger("cockpit-trigger-intake")


# ── The two scopes this module works in ─────────────────────────────────────────────────────────────


@contextmanager
def fleet_session() -> Iterator[Session]:
    """A fresh session reading every org: for the fleet-wide questions, "which trigger owns this ingress token?"
    (a public route, nothing bound), "which triggers have work?" (the processor) and "does another org's Slack
    trigger hold this channel?" (a save). Only ids leave it; every read and write about a trigger then runs in
    `trigger_session`."""
    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        yield session


@contextmanager
def trigger_session(trigger_id: UUID) -> Iterator[tuple[Session, UUID]]:
    """A fresh session bound to `trigger_id`'s own org (derived from the row), and that org. Every write this
    module makes — a delivery row, a counter, a filed Task — happens in one of these, so it lands in the
    trigger's org and can reach nothing outside it."""
    with SessionLocal() as session, seam.detached(), seam.bind_from(session, TaskTrigger, trigger_id) as org:
        yield session, org

# ── Preflight seam (§3.6) ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreflightInput:
    """What a preflight sees: the redacted event, the trigger's state document, and this trigger's most recent
    dedup keys (a hint so a preflight can skip known items; the dedup store stays authoritative). NEVER the
    secret (AC-8.3)."""

    event: dict[str, Any]
    state: dict[str, Any]
    seen_keys: list[str]


@dataclass(frozen=True)
class PreflightResult:
    run: Optional[bool] = None
    reason: Optional[str] = None
    items: Optional[list[dict[str, Any]]] = None
    new_state: Optional[dict[str, Any]] = None


class PreflightError(Exception):
    """The preflight said something unusable (an error, a timeout, malformed output). Counts as a failure; the
    message is the readable reason the log shows."""


class PreflightUnavailable(Exception):
    """The preflight's venue could not be reached. Retried, not counted, until `UNAVAILABLE_WINDOW` passes."""


class PreflightPreparing(PreflightUnavailable):
    """The venue is preparing to run it — a node's first clone of the project's mirror. Retried, not counted,
    until `preparing_window()` passes: that clone may run for the whole operator clone budget."""


PreflightRunner = Callable[[Session, TaskTrigger, PreflightInput], PreflightResult]
PREFLIGHT_RUNNERS: dict[str, PreflightRunner] = {}


def register_preflight(kind: str, runner: PreflightRunner) -> None:
    PREFLIGHT_RUNNERS[kind] = runner


def _no_preflight(_session: Session, _trigger: TaskTrigger, _input: PreflightInput) -> PreflightResult:
    return PreflightResult(run=True)


register_preflight(PREFLIGHT_NONE, _no_preflight)

# At most four preflights in flight per API replica; the pass hands rows here and never waits.
PREFLIGHT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="trigger-preflight")
# After this long of an unreachable venue, a row stops retrying and becomes a counted failure.
UNAVAILABLE_WINDOW = timedelta(minutes=10)
# The claim outlives the call by this much, so a slow apply is not mistaken for a crashed replica.
CLAIM_SLACK = timedelta(seconds=60)
SEEN_KEYS_LIMIT = 1000
# A preflight that returns more items than this is broken, not busy.
MAX_ITEMS = 100


PREPARING_REASON = "preflight preparing, retrying: "


def preparing_window() -> timedelta:
    """How long a `PreflightPreparing` row keeps retrying: the node's clone budget, then the usual window for the
    preflight that follows it."""
    return timedelta(seconds=settings_reads.get_int("node_clone_timeout_secs")) + UNAVAILABLE_WINDOW


def parse_preflight_output(text: str) -> PreflightResult:
    """The R8 grammar — `{"run": bool, "reason"?: str}` or `{"items": [{"id": ..., ...}]}`, plus an optional
    `"state": {...}` — or `PreflightError` naming what is wrong. Shared by every preflight kind, so they reject
    the same malformed output."""
    if len((text or "").encode()) > MAX_PREFLIGHT_OUTPUT_BYTES:
        raise PreflightError("preflight output is over 64 KiB")
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PreflightError(f"preflight output is not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise PreflightError("preflight output must be a JSON object")
    unknown = set(data) - {"run", "reason", "items", "state"}
    if unknown:
        raise PreflightError(f"preflight output has unknown keys: {', '.join(sorted(unknown))}")
    has_run, has_items = "run" in data, "items" in data
    if has_run == has_items:
        raise PreflightError('preflight output must carry exactly one of "run" or "items"')
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise PreflightError('"reason" must be a string')
    state = data.get("state")
    if state is not None:
        if not isinstance(state, dict):
            raise PreflightError('"state" must be a JSON object')
        if len(json.dumps(state).encode()) > MAX_STATE_BYTES:
            raise PreflightError("preflight state is over 64 KiB — the previous state was kept")
    if has_run:
        if not isinstance(data["run"], bool):
            raise PreflightError('"run" must be true or false')
        return PreflightResult(run=data["run"], reason=reason, new_state=state)
    items = data["items"]
    if not isinstance(items, list):
        raise PreflightError('"items" must be a list')
    if len(items) > MAX_ITEMS:
        raise PreflightError(f"preflight returned {len(items)} items; at most {MAX_ITEMS} are accepted")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PreflightError(f"item {index} must be a JSON object")
        item_id = item.get("id")
        if isinstance(item_id, bool) or not isinstance(item_id, (str, int)) or str(item_id).strip() == "":
            raise PreflightError(f'item {index} needs a non-empty "id" (a string or a number)')
    return PreflightResult(items=items, reason=reason, new_state=state)


# ── Ingest helpers ────────────────────────────────────────────────────────────────────────────────────

_DROPPED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
_DROPPED_HEADER_PREFIXES = ("x-hub-signature", "x-jaah-signature")


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The headers as stored: lowercased names, minus every credential a sender proves itself with."""
    return {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _DROPPED_HEADERS and not str(name).lower().startswith(_DROPPED_HEADER_PREFIXES)
    }


def parse_body(raw: bytes) -> Any:
    """The body as JSON, or `{"raw": text}` when it is not JSON (so `{{body.raw}}` still reaches it)."""
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return {"raw": text}


def build_event(raw: bytes, headers: Mapping[str, str], query: Mapping[str, str]) -> dict[str, Any]:
    return {"body": parse_body(raw), "headers": redact_headers(headers), "query": dict(query)}


def preview_of(event: Any) -> dict[str, Any]:
    """A readable copy for the deliveries log, at most `MAX_PREVIEW_BYTES` (§3.1)."""
    text = json.dumps(event, ensure_ascii=False, indent=1, default=str)
    encoded = text.encode()
    if len(encoded) <= MAX_PREVIEW_BYTES:
        return {"text": text, "truncated": False}
    return {"text": encoded[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore"), "truncated": True}


def dedup_key_for(value: Any) -> str:
    """A dedup key from an item id or a natural id; a key longer than the column cap is hashed."""
    key = str(value).strip()
    if len(key) <= MAX_DEDUP_KEY_CHARS:
        return key
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def natural_dedup_key(raw: bytes, headers: Mapping[str, str], auth: str = "hmac") -> str:
    """An HTTP delivery's natural id (A13): GitHub's `X-GitHub-Delivery` for a GitHub trigger (its Redeliver
    resends the same id, AC-5.2), else the sender's `X-Jaah-Delivery-Id`; failing either, the body's SHA-256."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    header = GITHUB_DELIVERY_HEADER if auth == AUTH_GITHUB else DELIVERY_ID_HEADER
    delivery_id = str(lowered.get(header, "")).strip()
    if delivery_id:
        return dedup_key_for(delivery_id)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ── The webhook route's body (`routes/public.py::trigger_ingress`) ──────────────────────────────────

# Per token 120/min, per address 600/min (§3.4 step 3), shared across replicas.
_TOKEN_LIMITER = SharedRateLimiter(limit=120, window=60.0, bucket="trigger-ingress")
_IP_LIMITER = SharedRateLimiter(limit=600, window=60.0, bucket="trigger-ingress-ip")

REASON_OVERSIZE = "body over 1 MiB"
REASON_BAD_SIGNATURE = "invalid signature"
REASON_BAD_KEY = "invalid API key"
REASON_KEY_ADDRESS = "API key not allowed from this address"
REASON_KEY_LACKS_CREATE = "API key lacks tasks:create"


@dataclass(frozen=True)
class IngressAnswer:
    status: int
    body: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class KeyRefusal:
    """Why `_key_verdict` refused a delivery's API key: the status and `detail` to answer, the `reason` to log."""

    status: int
    detail: str
    reason: str


def _resolve_token(token: str) -> Optional[tuple[UUID, str]]:
    """(trigger id, auth scheme) for an HTTP trigger's ingress token, read fleet-wide (nothing is bound on a
    public route, and the token is what tells us the org). A `github` trigger's scheme is GitHub's own."""
    if not token or len(token) > 128:
        return None
    with fleet_session() as session:
        row = session.execute(
            select(TaskTrigger.id, TaskTrigger.source_kind, TaskTrigger.source_config).where(
                TaskTrigger.ingress_token == token, TaskTrigger.source_kind.in_(HTTP_SOURCE_KINDS)
            )
        ).first()
    if row is None:
        return None
    if row.source_kind == SOURCE_GITHUB:
        return row.id, AUTH_GITHUB
    return row.id, trigger_secrets.webhook_auth(row.source_config)


def _log_delivery(trigger_id: UUID, *, state: str, reason: Optional[str], event: Optional[dict] = None,
                  dedup_key: Optional[str] = None, slack_event_id: Optional[str] = None,
                  api_key_id: Optional[UUID] = None) -> int:
    """Insert one delivery row for `trigger_id`, in the trigger's own org, and commit. Returns its id.

    Never writes the trigger row: the processor holds that row's lock while it files, and an ingest must neither
    wait on it nor make the processor skip the trigger. `last_delivery_at` is stamped by the processor and served
    from the deliveries themselves (`routes/triggers.py`)."""
    now = datetime.now(timezone.utc)
    with trigger_session(trigger_id) as (session, org):
        row = TriggerDelivery(
            trigger_id=trigger_id,
            org_id=org,
            state=state,
            reason=reason,
            event=event,
            preview=preview_of(event) if event is not None else None,
            dedup_key=dedup_key,
            slack_event_id=slack_event_id,
            api_key_id=api_key_id,
            processed_at=None if state == RECEIVED else now,
        )
        session.add(row)
        session.commit()
        return row.id


def ingest_http(token: str, raw: Optional[bytes], headers: Mapping[str, str], query: Mapping[str, str],
                client_ip: str) -> IngressAnswer:
    """§3.4's checks, in this exact order. `raw` is None when the body was over `MAX_BODY_BYTES`.

    1. size → 413 (logged `skipped` when the token resolves and its budget allows)
    2. token → 404, nothing logged
    3. abuse rate → 429, nothing logged (a flood cannot fill the table)
    4. API key (or a legacy signature/bearer) → 401 (403: the key's address or permission) + one `skipped` row per
       request; never a counted failure (A6)
    5. paused or failed → 202 + a `skipped` row
    6. accept → a `received` row, commit, fire the processor early → 202 `{delivery_id}`
    """
    if raw is None or len(raw) > MAX_BODY_BYTES:
        resolved = _resolve_token(token)
        if resolved is not None and _TOKEN_LIMITER.allow(token):
            _log_delivery(resolved[0], state=SKIPPED, reason=REASON_OVERSIZE)
        return IngressAnswer(413, {"detail": "The body is over 1 MiB."})
    resolved = _resolve_token(token)
    if resolved is None:
        return IngressAnswer(404, {"detail": "Not found."})
    trigger_id, auth = resolved
    if not _TOKEN_LIMITER.allow(token) or not _IP_LIMITER.allow(client_ip or "unknown"):
        return IngressAnswer(429, {"detail": "Too many requests."})
    verdict = _verify(trigger_id, auth, raw, headers)
    if verdict is None:
        # Deleted between the token lookup and here.
        return IngressAnswer(404, {"detail": "Not found."})
    authentic, stopped, preflighted, org_id = verdict
    api_key_id: Optional[UUID] = None
    if auth == AUTH_API_KEY:
        key = _key_verdict(org_id, headers, client_ip)
        if isinstance(key, KeyRefusal):
            _log_delivery(trigger_id, state=SKIPPED, reason=key.reason)
            return IngressAnswer(key.status, {"detail": key.detail})
        api_key_id = key.key_id
    elif not authentic:
        _log_delivery(trigger_id, state=SKIPPED, reason=REASON_BAD_SIGNATURE)
        return IngressAnswer(401, {"detail": "Invalid signature."})
    event = build_event(raw, headers, query)
    if stopped is not None:
        delivery_id = _log_delivery(trigger_id, state=SKIPPED, reason=stopped, event=event, api_key_id=api_key_id)
        return IngressAnswer(202, {"delivery_id": delivery_id, "skipped": stopped})
    # The natural key only when there is no preflight (§3.4): a preflight decides what is new itself, and its
    # items carry their own ids.
    delivery_id = _log_delivery(
        trigger_id, state=RECEIVED, reason=None, event=event,
        dedup_key=None if preflighted else natural_dedup_key(raw, headers, auth), api_key_id=api_key_id,
    )
    fire_intake()
    return IngressAnswer(202, {"delivery_id": delivery_id})


def _verify(trigger_id: UUID, auth: str, raw: bytes, headers: Mapping[str, str]
            ) -> Optional[tuple[bool, Optional[str], bool, UUID]]:
    """(authentic, stopped reason, has a preflight, org) for the request, or None when the trigger is gone. The secret
    is opened, compared in constant time and dropped inside this function; it never leaves it. An `api_key` trigger
    has no secret (never authentic here): its key is `_key_verdict`'s."""
    try:
        with trigger_session(trigger_id) as (session, org):
            trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id))
            if trigger is None:
                return None
            authentic = False
            if auth != AUTH_API_KEY:
                try:
                    secret = trigger_secrets.open_secret(trigger)
                except ValueError:
                    secret = None
                authentic = secret is not None and trigger_secrets.verify(auth, secret, raw, _Headers(headers))
                del secret
            return (authentic, trigger_health.stopped_reason(trigger), trigger.preflight_kind != PREFLIGHT_NONE,
                    org)
    except seam.TenancyRowMissing:
        return None


def _key_verdict(org_id: UUID, headers: Mapping[str, str], client_ip: str
                 ) -> "api_keys.ApiKeyContext | KeyRefusal":
    """The Jaah API key an `api_key` webhook delivery presents (`Authorization: Bearer jaah_…`), verified as any API
    request's (`api_keys.verify`: revoked, expired, its owner's hold, both IP allowlists), or the refusal to answer,
    the `detail` to answer and the `reason` to log. The key must be of the trigger's own org, and
    narrowed to it must hold `tasks:create`. A key of another org answers as an unknown one."""
    from app import api_keys
    from app.authz.catalog import permission_key
    from app.authz.principal import RevokedSession, key_principal

    invalid = KeyRefusal(401, "Invalid API key.", REASON_BAD_KEY)
    token = api_keys.bearer_value(_Headers(headers).get("authorization"))
    if token is None or not api_keys.parse(token):
        return invalid
    key = api_keys.verify(token, client_ip)
    if isinstance(key, api_keys.Refusal):
        # Audited in the key's own org, as the middleware audits a refused API request: a dead key still in use is a
        # leak sign its org's admins watch for.
        api_keys.record_refusal(key, client_ip=client_ip)
        if key.status == 403 and key.org_id == org_id:
            return KeyRefusal(403, "The API key is not allowed from this address.", REASON_KEY_ADDRESS)
        return invalid
    if key.org_id != org_id:
        api_keys.record_refusal(api_keys.Refusal(401, "trigger_org", key.key_id, key.name, key.owner_id, key.org_id),
                                client_ip=client_ip)
        return invalid
    try:
        principal = key_principal(key)
    except RevokedSession:
        return invalid
    if not principal.has(permission_key("tasks", "create")):
        return KeyRefusal(403, "The API key lacks Tasks: create.", REASON_KEY_LACKS_CREATE)
    return key


class _Headers:
    """A case-insensitive `get` over a plain mapping (a Starlette `Headers` already is one)."""

    def __init__(self, headers: Mapping[str, str]):
        self._lowered = {str(k).lower(): v for k, v in headers.items()}

    def get(self, name: str, default: str = "") -> str:
        return self._lowered.get(name.lower(), default)


def fire_intake() -> None:
    """Fire the processor now (never raises; the interval is the backstop)."""
    from app.cron_runtime import fire_now_activity_sync

    fire_now_activity_sync(INTAKE_ACTIVITY)


# ── The Slack mention source (`slack_inbound._handle_app_mention`, architecture §4 "Slack (C5)") ────────

# A leading user mention, `<@U0123>` or `<@U0123|jaah>` — the bot's, when its id is unknown.
_LEADING_MENTION = re.compile(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>")
REASON_NO_SLACK_TRIGGER = "no Slack trigger covers this channel"
#: The trigger's org's plan leaves Slack out (`plan_terms.SLACK`): its mentions are logged `skipped`.
REASON_SLACK_OFF_PLAN = "Slack isn't on the plan"
# Slack replies run here, so a slow Slack never holds the processor pass past its timeout.
SLACK_REPLY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trigger-slack-reply")
# What the channel is told when a mention files nothing. Fixed sentences: the logged reason can carry an org
# name, an owner's permissions or an exception, and the channel may hold guests (the console shows the detail).
SLACK_UNLINKED_REPLY = (
    "I didn't file a Task: your Slack account isn't linked to a member of the organization this channel's "
    "trigger belongs to. Connect Slack in your Jaah notification settings, then mention me again."
)
SLACK_RATE_CAPPED_REPLY = "I didn't file a Task: this channel's trigger has reached its hourly limit."
SLACK_NOT_FILED_REPLY = (
    "I didn't file a Task — an org admin can see why in the trigger's deliveries under Automation › Triggers."
)


@dataclass(frozen=True)
class SlackIntake:
    """What `ingest_slack` did: `state` is `received`, `skipped` (a stopped trigger took it) or `unmatched`;
    `reason` is the logged reason; `delivery_id` the row it wrote."""

    state: str
    delivery_id: int
    reason: Optional[str] = None


def strip_mention(text: str, bot_user_id: str = "") -> str:
    """The message without `@Jaah` — what `{{slack.text}}` renders. With the bot's id, every mention of it is
    dropped and other people's stay; without it, only the leading mention (an `app_mention` names the bot)."""
    text = text or ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    else:
        text = _LEADING_MENTION.sub("", text, count=1)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def slack_dedup_key(channel: str, ts: str) -> str:
    """A mention's natural id (§3.4): `<channel>:<ts>` names one Slack message."""
    return dedup_key_for(f"{channel}:{ts}")


def _slack_trigger_for(channel: str) -> Optional[UUID]:
    """The first `slack` trigger, by id, whose `channel_ids` holds `channel` — paused or failed included, so a
    stopped trigger takes its channel's mentions rather than letting them fall through (§4). A mention in a
    tenant's own workspace matches only that org's triggers, one in the home workspace only the operator
    plane's (`slack_workspaces`)."""
    if not edition_features.enabled("slack"):
        # No Slack in this edition, so no mention is ever a trigger's.
        return None
    from app import slack_workspaces
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    workspace = slack_workspaces.current()
    with fleet_session() as session:
        query = select(TaskTrigger.id).where(
            TaskTrigger.source_kind == SOURCE_SLACK,
            TaskTrigger.source_config["channel_ids"].contains([channel]),
        )
        if workspace is not None and workspace.is_home:
            # The home workspace serves the operator plane only (`slack_workspaces`), so its mentions never file
            # a tenant's Task.
            query = query.where(TaskTrigger.org_id.in_(OPERATOR_PLANE_ORG_IDS))
        elif workspace is not None:
            query = query.where(TaskTrigger.org_id == workspace.org_id)
        return session.scalar(query.order_by(TaskTrigger.id).limit(1))


def slack_channel_held_by_another_org(channels: list[str], org_id: UUID) -> Optional[str]:
    """The first of `channels` a `slack` trigger outside `org_id` already holds, or None. Mentions route
    fleet-wide by channel (`_slack_trigger_for`), so a channel belongs to one org's triggers; the save path
    refuses a second org's claim. Same-org overlap is allowed — first by id takes it."""
    if not channels:
        return None
    with fleet_session() as session:
        configs = session.scalars(
            select(TaskTrigger.source_config).where(
                TaskTrigger.source_kind == SOURCE_SLACK,
                TaskTrigger.org_id != org_id,
                or_(*(TaskTrigger.source_config["channel_ids"].contains([c]) for c in channels)),
            )
        ).all()
    held = {channel for config in configs for channel in (config or {}).get("channel_ids") or ()}
    return next((channel for channel in channels if channel in held), None)


def _log_unmatched(*, preview: dict[str, Any], slack_event_id: str) -> int:
    """An `unmatched` row in org zero: the Slack app is deployment-global, so a mention no trigger takes is
    the operator's to see (`GET /api/triggers/unmatched`). Bodiless: nothing will ever render it."""
    with SessionLocal() as session, seam.detached(), seam.bound_to(ORG_ZERO_ID):
        row = TriggerDelivery(
            trigger_id=None, state=UNMATCHED, reason=REASON_NO_SLACK_TRIGGER,
            preview=preview_of(preview), slack_event_id=slack_event_id,
            processed_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return row.id


def ingest_slack(event: Mapping[str, Any], *, event_id: str = "", bot_user_id: str = "",
                 permalink: Optional[Callable[[str, str, UUID], str]] = None) -> Optional[SlackIntake]:
    """One `app_mention` as one delivery row, or None when it names no channel/ts. `permalink(channel, ts, org)`
    is asked only for a mention a trigger takes, `org` being the trigger's. A stopped trigger logs `skipped`; an accepted mention is
    `received`, committed, and the processor fired early so the Task and its reply land within seconds."""
    channel = str(event.get("channel") or "")
    ts = str(event.get("ts") or "")
    if not channel or not ts:
        return None
    slack = {
        "text": strip_mention(str(event.get("text") or ""), bot_user_id),
        "user": str(event.get("user") or ""),
        "channel": channel,
        "permalink": "",
        "ts": ts,
        "thread_ts": str(event.get("thread_ts") or ""),
    }
    slack_event_id = event_id or f"{channel}:{ts}"
    trigger_id = _slack_trigger_for(channel)
    if trigger_id is None:
        row_id = _log_unmatched(preview={"slack": slack}, slack_event_id=slack_event_id)
        return SlackIntake(UNMATCHED, row_id, REASON_NO_SLACK_TRIGGER)
    from app import plan_terms

    try:
        with trigger_session(trigger_id) as (session, org):
            trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id))
            stopped = trigger_health.stopped_reason(trigger) if trigger is not None else None
            # The trigger's org's plan leaves Slack out: the mention is logged, never filed.
            if stopped is None and not plan_terms.switch_on(session, org, plan_terms.SLACK):
                stopped = REASON_SLACK_OFF_PLAN
            preflighted = trigger is not None and trigger.preflight_kind != PREFLIGHT_NONE
    except seam.TenancyRowMissing:
        # Deleted between the match and here: as if it had never matched.
        row_id = _log_unmatched(preview={"slack": slack}, slack_event_id=slack_event_id)
        return SlackIntake(UNMATCHED, row_id, REASON_NO_SLACK_TRIGGER)
    if stopped is not None:
        row_id = _log_delivery(trigger_id, state=SKIPPED, reason=stopped, event={"slack": slack},
                               slack_event_id=slack_event_id)
        return SlackIntake(SKIPPED, row_id, stopped)
    if permalink is not None:
        slack["permalink"] = permalink(channel, ts, org)
    row_id = _log_delivery(
        trigger_id, state=RECEIVED, reason=None, event={"slack": slack}, slack_event_id=slack_event_id,
        dedup_key=None if preflighted else slack_dedup_key(channel, ts),
    )
    fire_intake()
    return SlackIntake(RECEIVED, row_id)


def _slack_thread(trigger: TaskTrigger, event: Optional[Mapping[str, Any]]) -> Optional[tuple[str, str, UUID]]:
    """(channel, thread ts, the trigger's org) a reply about this delivery goes to, or None when it is not a Slack
    trigger's mention — decided by the trigger's kind, never by the event's shape, so no other sender can aim a
    reply. The org's plan gates the reply (`slack_egress.slack_api_post`)."""
    if trigger.source_kind != SOURCE_SLACK:
        return None
    slack = (event or {}).get("slack") if isinstance(event, Mapping) else None
    if not isinstance(slack, Mapping) or not slack.get("channel") or not slack.get("ts"):
        return None
    return str(slack["channel"]), str(slack.get("thread_ts") or slack["ts"]), trigger.org_id


def _reply_in_slack(where: Optional[tuple[str, str, UUID]], text: str) -> None:
    """Post one threaded reply about a Slack delivery on `SLACK_REPLY_POOL` (never raises, never waits;
    `slack_inbound.post_reply` owns how)."""
    if where is None:
        return
    if not edition_features.enabled("slack"):
        # No Slack in this edition, so nowhere to reply.
        return
    from app import slack_inbound

    SLACK_REPLY_POOL.submit(slack_inbound.post_reply, where[0], where[1], text, org_id=where[2])


def _slack_reply_for(state: str) -> str:
    if state == FILTERED:
        return SLACK_UNLINKED_REPLY
    if state == RATE_CAPPED:
        return SLACK_RATE_CAPPED_REPLY
    return SLACK_NOT_FILED_REPLY


def _slack_sender_refusal(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> Optional[str]:
    """Why a Slack mention's sender may not file into this trigger's org (A12), or None. The sender must have
    linked their Slack account to an enabled member of the trigger's org."""
    from app import notify_targets
    from app.models.orgs import Org

    slack = (row.event or {}).get("slack") or {}
    members = [
        user for user in notify_targets.users_for_slack_member_id(session, str(slack.get("user") or ""))
        if user.org_id == trigger.org_id and not user.is_system and user.disabled_at is None
    ]
    if members:
        return None
    org = session.get(Org, trigger.org_id)
    return f"Slack user not linked to a member of {org.name if org is not None else 'this organization'}"


# ── The GitHub source (architecture §4 "GitHub (C4)") ──────────────────────────────────────────────────

REASON_GITHUB_PING = "GitHub ping — webhook connected"


#: A sender's value in a reason is bounded (the body is signed, not trusted to be short).
_VALUE_CHARS = 100


def github_filter_reason(config: Mapping[str, Any], event: Mapping[str, Any]) -> Optional[str]:
    """Why a GitHub delivery files nothing, readably, or None when it matches the trigger's `events`, `actions`
    (when any are set) and `repository` (when set). Decided from the delivery alone: no GitHub read."""
    headers = event.get("headers") or {}
    body = event.get("body") if isinstance(event.get("body"), dict) else {}
    name = str(headers.get(GITHUB_EVENT_HEADER, "")).strip()
    if name == "ping":
        return REASON_GITHUB_PING
    if not name:
        return "not a GitHub delivery: no X-GitHub-Event header"
    if not body or set(body) == {"raw"}:
        # A form-encoded delivery signs fine but carries no JSON to match against.
        return "body is not JSON — set the webhook's Content type to application/json"
    if name not in (config.get("events") or []):
        return f"event {one_line(name, _VALUE_CHARS)} not configured"
    actions = config.get("actions") or []
    action = body.get("action")
    if actions and action not in actions:
        return (f"event {one_line(name, _VALUE_CHARS)} action {one_line(action, _VALUE_CHARS)} not configured"
                if action else f"event {one_line(name, _VALUE_CHARS)} has no action to match")
    repository = config.get("repository")
    repo = body.get("repository") if isinstance(body.get("repository"), dict) else {}
    full_name = str(repo.get("full_name") or "")
    if repository and full_name.lower() != str(repository).lower():
        return f"repository {one_line(full_name, _VALUE_CHARS) or '(none)'} not configured (expected {repository})"
    return None


# ── Email (architecture §4 "Gmail (C6)"; system inbound: `app/ses_inbound.py`) ─────────────────────────────

REASON_NO_GMAIL_TRIGGER = "no email trigger matches this message"
#: A rejected message's Unmatched reason, by `inbound_emails.rejected_reason` (`ses_inbound.py` owns the rule).
REASON_REJECTED = {
    "spam": "rejected: SES marked it spam",
    "virus": "rejected: SES found a virus",
    "dmarc": "rejected: failed DMARC from a sender that publishes p=reject",
    "rate_limited": f"rejected: over {DAILY_ROUTED_LIMIT} messages a day from this sender",
    "plan_off": "rejected: the org's plan does not include Tenant email",
}
# How far back a pass retries a message whose routing never finished (a crash or a failed transaction after the
# store committed). Bounded so a retry never routes mail stored long before.
ROUTE_RETRY_WINDOW = timedelta(hours=2)
#: An email trigger's `inbox` that reads the deployment's own mail domain (`trigger_config.GmailConfig`).
SYSTEM_INBOX = "system"


def from_rule_matches(rule: str, from_address: Optional[str]) -> bool:
    """Whether a lower-cased `from` rule matches a message's From: a whole address (`boss@victim.example`) only that
    address, a domain (`@victim.example` or `victim.example`) that domain and its subdomains, anything else as a substring.
    A substring would let `boss@victim.example.evil.example`, which passes DMARC for its own domain, pass for `victim.example`."""
    sender = parseaddr(from_address or "")[1].strip().lower()
    # A From naming several mailboxes, or none, never satisfies an address or a domain: which one DMARC proved is unknown.
    sender = sender if sender.count("@") == 1 else ""
    local, at, domain = rule.rpartition("@")
    if at and local and domain:
        return sender == rule
    wanted_domain = (domain if at else rule).strip(".")
    if wanted_domain and "." in wanted_domain and not any(c.isspace() or c == "@" for c in wanted_domain):
        sender_domain = sender.rpartition("@")[2] if sender else ""
        return bool(sender_domain) and (sender_domain == wanted_domain or sender_domain.endswith("." + wanted_domain))
    return rule in (from_address or "").lower()


def email_matches(config: Optional[Mapping[str, Any]], email: InboundEmail) -> bool:
    """True when every rule field the email trigger sets matches the message (D11): To and Subject as a
    case-insensitive substring, From by `from_rule_matches`. A rule with no field set matches every message at its own
    hosted address or Gmail connection (`reads_inbox` already narrowed it there); on the shared system domain the config
    refuses it."""
    fields = (("to", email.to_address), ("from", email.from_address), ("subject", email.subject))
    wanted = [(str((config or {}).get(key) or "").strip().lower(), value) for key, value in fields]
    # A `from` rule reads as "only this sender": a message's From counts only when DMARC proved it
    # (`ses_inbound.message_fields`, for SES and tenant Gmail alike), so a forged From never fires a sender-scoped
    # trigger. The From we parsed must also align with the domain that authenticated it (`sender_domain`): we read
    # `boss@victim.example <a@evil.example>` as `boss@victim.example`, a receiver reading the angle-addr proves `evil.example`.
    # Only a Gmail-era row (`provider = 'gmail'`) predates the verdicts.
    verdicts = email.verdicts or {}
    if wanted[1][0] and email.provider != "gmail" and not (
            verdicts.get("from_authenticated")
            and aligned(from_domain(email.from_address or ""), str(verdicts.get("sender_domain") or ""))):
        return False
    if not any(needle for needle, _ in wanted):
        return bool((config or {}).get("address_id")) or str((config or {}).get("inbox") or SYSTEM_INBOX) != SYSTEM_INBOX
    (to_rule, to_value), (from_rule, from_value), (subject_rule, subject_value) = wanted
    return ((not to_rule or to_rule in (to_value or "").lower())
            and (not from_rule or from_rule_matches(from_rule, from_value))
            and (not subject_rule or subject_rule in (subject_value or "").lower()))


def reads_inbox(config: Optional[Mapping[str, Any]], email: InboundEmail) -> bool:
    """Whether a trigger listens where this message arrived (seam S8): a trigger naming an `address_id` reads that
    hosted address alone; otherwise `inbox` — `system` (the default) the deployment's own domain, a connection's
    id that tenant Gmail mailbox."""
    config = config or {}
    address_id = str(config.get("address_id") or "")
    if address_id:
        return email.source_ref == address_id
    return email.source_ref == str(config.get("inbox") or SYSTEM_INBOX)


def _email_event(email: InboundEmail) -> dict[str, Any]:
    """What `{{email.*}}` renders: the stored readable text, never raw MIME."""
    return {"email": {
        "from": email.from_address, "to": email.to_address, "subject": email.subject, "body": email.body_text,
    }}


def _email_dedup_key(email: InboundEmail) -> str:
    # A Gmail-era row keeps the key it was always filed under.
    if email.gmail_message_id:
        return dedup_key_for(email.gmail_message_id)
    return dedup_key_for(f"{email.provider}:{email.provider_message_id}:{email.recipient}")


def _route_email(email_id: UUID, org_id: UUID, trigger_id: Optional[UUID] = None) -> Optional[str]:
    """Route one stored message: the outcome it was logged with, or None when it was already routed (or another
    pass holds it). One transaction in the message's org — every email trigger it may reach is that org's (AD-I2) —
    so the delivery row and the `trigger_routed_at` stamp land together, and a retry never routes it twice. A
    rejected message is logged unmatched with its reason and reaches no trigger; `trigger_id` pins a tenant
    address to one trigger (seam S5)."""
    with SessionLocal() as session, seam.detached(), seam.bound_to(org_id):
        email = session.scalar(
            select(InboundEmail)
            .where(InboundEmail.id == email_id, InboundEmail.trigger_routed_at.is_(None))
            .with_for_update(skip_locked=True)
        )
        if email is None:
            return None
        trigger = None
        if not email.rejected_reason:
            # Every email trigger, paused or failed included and by id (A11): a stopped one takes its mail rather
            # than letting it fall through to a later rule (§4).
            statement = select(TaskTrigger).where(TaskTrigger.source_kind == SOURCE_GMAIL, TaskTrigger.org_id == org_id)
            if trigger_id is not None:
                statement = statement.where(TaskTrigger.id == trigger_id)
            triggers = session.scalars(statement.order_by(TaskTrigger.id)).all()
            # A pinned trigger is the address's own: its inbox is the resolver's answer, its rule still applies.
            trigger = next(
                (t for t in triggers
                 if (trigger_id is not None or reads_inbox(t.source_config, email))
                 and email_matches(t.source_config, email)),
                None,
            )
        now = datetime.now(timezone.utc)
        event = _email_event(email)
        if trigger is None:
            # Bodiless, like an unmatched Slack mention: who sent it and what about is enough to see it was missed,
            # and mail no rule took is not copied out of `inbound_emails` again.
            headline = {"email": {k: v for k, v in event["email"].items() if k != "body"}}
            reason = (REASON_REJECTED.get(email.rejected_reason, f"rejected: {email.rejected_reason}")
                      if email.rejected_reason else REASON_NO_GMAIL_TRIGGER)
            row = TriggerDelivery(
                trigger_id=None, state=UNMATCHED, reason=reason, preview=preview_of(headline),
                inbound_email_id=email.id, processed_at=now,
            )
        else:
            stopped = trigger_health.stopped_reason(trigger)
            preflighted = trigger.preflight_kind != PREFLIGHT_NONE
            row = TriggerDelivery(
                trigger_id=trigger.id, state=SKIPPED if stopped else RECEIVED, reason=stopped,
                event=event, preview=preview_of(event), inbound_email_id=email.id,
                dedup_key=None if stopped or preflighted else _email_dedup_key(email),
                processed_at=now if stopped else None,
            )
        session.add(row)
        email.trigger_routed_at = now
        session.commit()
        return row.state


def unrouted_emails(now: Optional[datetime] = None) -> list[tuple[UUID, UUID]]:
    """Every org's messages stored within `ROUTE_RETRY_WINDOW` that no routing pass has stamped, oldest first, as
    (id, org) pairs: system mail, hosted-address mail and tenant Gmail alike (each is routed in its own org)."""
    since = (now or datetime.now(timezone.utc)) - ROUTE_RETRY_WINDOW
    with seam.operator_scope(), SessionLocal() as session:
        return [(row.id, row.org_id) for row in session.execute(
            select(InboundEmail.id, InboundEmail.org_id)
            .where(InboundEmail.trigger_routed_at.is_(None), InboundEmail.stored_at >= since)
            .order_by(InboundEmail.stored_at, InboundEmail.id)
        )]


def route_emails(email_ids: list[UUID], *, org_id: UUID, trigger_id: Optional[UUID] = None) -> dict[str, int]:
    """Route each newly stored message of `org_id` (`ses_inbound.run`) to the first matching email trigger, or log
    it unmatched for the operator. Outcome → count. One message's fault never stops the rest; an accepted one
    fires the processor early so its Task lands well inside the 2-minute bound (AC-6.1)."""
    counts: dict[str, int] = {}
    for email_id in email_ids:
        try:
            outcome = _route_email(email_id, org_id, trigger_id)
        except Exception:  # noqa: BLE001 — logged; the next pass retries it (`unrouted_emails`)
            log.exception("trigger-intake: routing inbound email %s failed", email_id)
            continue
        if outcome is not None:
            counts[outcome] = counts.get(outcome, 0) + 1
    if counts.get(RECEIVED):
        fire_intake()
    return counts


# ── User registered: the control plane's own event ───────────────────────────────────────


def _user_triggers(session: Session, *, scope: str, moment: str, org: UUID) -> list[TaskTrigger]:
    """Every `user_registered` trigger of `org` watching `scope` at `moment`, by id — paused or failed included
    (they log `skipped`). An org-scope trigger has one moment, so `moment` only narrows platform ones; a config
    with no `scope` is `org` and no `moment` is `applied` (`trigger_config.UserRegisteredConfig`). Read under the
    caller's bound org; the explicit filter keeps the read honest should the caller's scope ever widen."""
    rows = session.scalars(
        select(TaskTrigger)
        .where(TaskTrigger.source_kind == SOURCE_USER_REGISTERED, TaskTrigger.org_id == org)
        .order_by(TaskTrigger.id)
    ).all()
    out = []
    for trigger in rows:
        config = trigger.source_config or {}
        if (config.get("scope") or USER_SCOPE_ORG) != scope:
            continue
        if scope != USER_SCOPE_ORG and (config.get("moment") or USER_MOMENT_APPLIED) != moment:
            continue
        out.append(trigger)
    return out


def _ingest_user_rows(triggers: list[TaskTrigger], event: dict[str, Any], dedup_key: str) -> dict[str, int]:
    """One delivery per trigger — EVERY one takes it, the event being its org's own, so no first-match routing.
    A stopped trigger logs `skipped`; an enabled one `received`, with the natural dedup key unless a preflight
    will split it (its items carry their own). Outcome → count; the processor is fired early for any accepted
    row. The rows are written under each trigger's own org (`_log_delivery`)."""
    targets = [(t.id, trigger_health.stopped_reason(t), t.preflight_kind != PREFLIGHT_NONE) for t in triggers]
    counts: dict[str, int] = {}
    for trigger_id, stopped, preflighted in targets:
        state = SKIPPED if stopped else RECEIVED
        try:
            _log_delivery(
                trigger_id, state=state, reason=stopped, event=event,
                dedup_key=None if stopped or preflighted else dedup_key_for(dedup_key),
            )
        except Exception:  # noqa: BLE001 — one trigger's fault never drops the event for the rest
            log.exception("trigger-intake: logging a registration for trigger %s failed", trigger_id)
            continue
        counts[state] = counts.get(state, 0) + 1
    if counts.get(RECEIVED):
        fire_intake()
    return counts


def ingest_platform_signup(event: dict[str, Any], *, moment: str, dedup_key: str) -> dict[str, int]:
    """A self-service sign-up's `event` (`trigger_user_registered`, after its commit) to the OPERATOR's platform
    triggers at `moment`. Org zero by the kind's own rule, as Gmail routing is: a sign-up belongs to no tenant
    yet, and the platform scope can only be created there (`routes/triggers.py::_source_allowed_or_refuse`)."""
    with SessionLocal() as session, seam.detached(), seam.bound_to(ORG_ZERO_ID):
        triggers = _user_triggers(session, scope=USER_SCOPE_PLATFORM, moment=moment, org=ORG_ZERO_ID)
    return _ingest_user_rows(triggers, event, dedup_key)


def ingest_member_joined(event: dict[str, Any], org: UUID, *, dedup_key: str) -> dict[str, int]:
    """A new member's `event` (`trigger_user_registered`, after the join committed) to the org-scope triggers of
    `org` — the org the caller just wrote the membership under, never a request field."""
    with SessionLocal() as session, seam.detached(), seam.bound_to(org):
        triggers = _user_triggers(session, scope=USER_SCOPE_ORG, moment=USER_MOMENT_APPLIED, org=org)
    return _ingest_user_rows(triggers, event, dedup_key)


# ── The processor ─────────────────────────────────────────────────────────────────────────────────────

# Rows one trigger may process in one pass, so one busy trigger cannot hold the activity past its timeout.
MAX_ROWS_PER_TRIGGER = 100
# How long one pass spends filing, inside the activity's 60 s bound (`cron_system_activities`): a fan-out still
# running at this point leaves its remaining targets owed, and the pass asks for a follow-up rather than overrun —
# so one org's All-projects trigger never holds every other org's deliveries behind the run lock.
PASS_BUDGET_SECONDS = 40.0
# Queued rows one pick looks through for one whose owed target has room (`_next_row`); `queue_depth` caps the queue
# at 100, so this sees all of it.
MAX_QUEUED_SCAN = 100
REASON_OWNER_LACKS_CREATE = "owner lacks tasks:create"
# Outcomes after which a row's stored event is never rendered again.
_BODILESS_OUTCOMES = frozenset({DEDUPED, RATE_CAPPED, SKIPPED, FILTERED, FAILED})


@dataclass
class PassSummary:
    triggers: int = 0
    rows: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    # `time.monotonic()` past which the pass files nothing more; `out_of_time` says it stopped there.
    deadline: float = field(default_factory=lambda: time.monotonic() + PASS_BUDGET_SECONDS)
    out_of_time: bool = False

    def count(self, outcome: str) -> None:
        self.rows += 1
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def detail(self) -> str:
        if not self.rows:
            return ""
        parts = ", ".join(f"{n} {state}" for state, n in sorted(self.outcomes.items()))
        return f"processed {self.rows} deliveries of {self.triggers} trigger(s): {parts}"


def triggers_with_work(session: Session, now: datetime) -> list[UUID]:
    """Triggers holding a row to process: `received`, `queued`, or a `preflighting`/`filing` row whose claim expired —
    or a filed Task to fold into health (`trigger_health`): one that settled and is not yet counted, or, for a
    trigger with a budget, one running or written to within the budget lookback. Triggers without a
    preflight first, so a slow model never delays a plain webhook."""
    filed = TriggerDelivery.state == DISPATCHED
    settled = Task.state.in_(_EPIC_SETTLED_STATES)
    rows = session.execute(
        select(TriggerDelivery.trigger_id, TaskTrigger.preflight_kind)
        .join(TaskTrigger, TaskTrigger.id == TriggerDelivery.trigger_id)
        .outerjoin(Task, Task.id == TriggerDelivery.task_id)
        .where(
            or_(
                TriggerDelivery.state.in_((RECEIVED, QUEUED)),
                TriggerDelivery.state.in_((PREFLIGHTING, FILING)) & (TriggerDelivery.claimed_until < now),
                filed & settled & TriggerDelivery.outcome_counted.is_(False),
                filed
                & TaskTrigger.budget_usd.is_not(None)
                & ((Task.state == ACTIVE) | (Task.updated_at >= now - trigger_health.BUDGET_LOOKBACK)),
            )
        )
        .distinct()
    ).all()
    return [row.trigger_id for row in sorted(rows, key=lambda r: (r.preflight_kind != PREFLIGHT_NONE, str(r.trigger_id)))]


def run_pass(*, now: Optional[datetime] = None) -> PassSummary:
    """One processor pass over every trigger with work (the `trigger-intake` activity's body)."""
    summary = PassSummary()
    with fleet_session() as session:
        trigger_ids = triggers_with_work(session, now or datetime.now(timezone.utc))
    for trigger_id in trigger_ids:
        if time.monotonic() >= summary.deadline:
            summary.out_of_time = True
            break
        try:
            process_trigger(trigger_id, summary)
        except Exception:  # noqa: BLE001 — one trigger's fault must not starve the rest
            log.exception("trigger-intake: trigger %s failed this pass", trigger_id)
    if summary.out_of_time:
        # The run lock is ours until we return; the dropped fire earns this pass's follow-up.
        fire_intake()
    return summary


def process_trigger(trigger_id: UUID, summary: Optional[PassSummary] = None) -> None:
    """Process one trigger's rows, one transaction per row, under that trigger's org."""
    summary = summary if summary is not None else PassSummary()
    summary.triggers += 1
    with trigger_session(trigger_id) as (session, org):
        _Processor(session, trigger_id, org, summary).run()


def trigger_owner(session: Session, trigger: TaskTrigger) -> Optional[AdminUser]:
    """The trigger's owner (its creator). Resolved as a caller, the way a cron's owner is: under the trigger's
    org bind an operator owner in org zero would read as missing."""
    with seam.identity_scope():
        owner = session.scalar(select(AdminUser).where(AdminUser.id == trigger.created_by_id))
    if owner is None or owner.is_system:
        return None
    return owner


class _Processor:
    def __init__(self, session: Session, trigger_id: UUID, org: UUID, summary: PassSummary):
        self.session = session
        self.trigger_id = trigger_id
        self.org = org
        self.summary = summary
        self.skip: set[int] = set()

    # ── the loop ──

    def run(self) -> None:
        session = self.session
        now = datetime.now(timezone.utc)
        # A crashed replica's preflight returns to the queue once its claim expires.
        session.execute(
            update(TriggerDelivery)
            .where(
                TriggerDelivery.trigger_id == self.trigger_id,
                TriggerDelivery.state == PREFLIGHTING,
                TriggerDelivery.claimed_until < now,
            )
            .values(state=RECEIVED, claimed_until=None)
        )
        trigger = self._lock()
        if trigger is None:
            session.rollback()
            return
        latest = session.scalar(
            select(func.max(TriggerDelivery.received_at)).where(TriggerDelivery.trigger_id == self.trigger_id)
        )
        if latest is not None:
            trigger.last_delivery_at = latest
        trigger_health.fold_filed_outcomes(session, trigger, now=now)
        trigger_health.fold_budget(session, trigger, now=now)
        session.commit()
        for _ in range(MAX_ROWS_PER_TRIGGER):
            if time.monotonic() >= self.summary.deadline:
                self.summary.out_of_time = True
                session.rollback()
                return
            trigger = self._lock()
            if trigger is None or self._filing_elsewhere():
                session.rollback()
                return
            row = self._next_row(trigger)
            if row is None:
                session.commit()
                return
            self._handle(trigger, row)

    def _lock(self) -> Optional[TaskTrigger]:
        """This trigger's row, locked for this transaction; None when another replica holds it (it is processing
        this trigger now) or the trigger was deleted."""
        return self.session.scalar(
            select(TaskTrigger).where(TaskTrigger.id == self.trigger_id).with_for_update(skip_locked=True)
        )

    def _filing_elsewhere(self) -> bool:
        """Another pass (a replica, or a Run now) is filing one of this trigger's deliveries under a live claim: the
        targets commit one by one, so the trigger's row lock no longer spans the firing — this does."""
        return self.session.scalar(
            select(TriggerDelivery.id).where(
                TriggerDelivery.trigger_id == self.trigger_id, TriggerDelivery.state == FILING,
                TriggerDelivery.claimed_until >= datetime.now(timezone.utc),
            ).limit(1)
        ) is not None

    def _next_row(self, trigger: TaskTrigger) -> Optional[TriggerDelivery]:
        """A crashed firing to resume, else a queued row with an owed target below its cap (FIFO), else the oldest
        received row."""
        from app import automation_fire

        session = self.session
        base = select(TriggerDelivery).where(TriggerDelivery.trigger_id == self.trigger_id)
        if self.skip:
            base = base.where(TriggerDelivery.id.not_in(self.skip))
        if trigger_health.is_enabled(trigger):
            stale = session.scalar(
                base.where(TriggerDelivery.state == FILING, TriggerDelivery.claimed_until < datetime.now(timezone.utc))
                .order_by(TriggerDelivery.id).limit(1).with_for_update(skip_locked=True)
            )
            if stale is not None:
                return stale
            automation = self._automation(trigger)
            queued = session.scalars(
                base.where(TriggerDelivery.state == QUEUED)
                .order_by(TriggerDelivery.id)
                .limit(MAX_QUEUED_SCAN)
                .with_for_update(skip_locked=True)
            ).all()
            for row in queued:
                if automation_fire.owed_ready(session, automation, row, trigger.concurrency_cap):
                    return row
        return session.scalar(
            base.where(TriggerDelivery.state == RECEIVED)
            .order_by(TriggerDelivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    # ── one row ──

    def _handle(self, trigger: TaskTrigger, row: TriggerDelivery) -> None:
        # A queued row, or a crashed firing's, is drained only while the trigger is enabled (`_next_row`), so only
        # received rows meet the health gate here.
        resuming = row.state in (QUEUED, FILING)
        if not resuming:
            stopped = trigger_health.stopped_reason(trigger)
            if stopped is not None:
                self._finish(trigger, row, SKIPPED, stopped)
                return
            # Before the owner's authority: a stranger's mention must neither learn nor trip it (A12).
            if trigger.source_kind == SOURCE_SLACK and row.parent_delivery_id is None:
                unlinked = _slack_sender_refusal(self.session, trigger, row)
                if unlinked is not None:
                    # Not even the preview stays: the sender is no member of this org, so their words are
                    # not this org's to read.
                    row.preview = None
                    self._finish(trigger, row, FILTERED, unlinked)
                    return
            if trigger.source_kind == SOURCE_GITHUB and row.parent_delivery_id is None:
                unwanted = github_filter_reason(trigger.source_config or {}, row.event or {})
                if unwanted is not None:
                    self._finish(trigger, row, FILTERED, unwanted)
                    return
            if trigger.source_kind == SOURCE_USER_REGISTERED and row.parent_delivery_id is None:
                from app.trigger_user_registered import user_filter_reason

                unwanted = user_filter_reason(trigger.source_config or {}, row.event or {})
                if unwanted is not None:
                    self._finish(trigger, row, FILTERED, unwanted)
                    return
        # Every filing re-asks the owner's authority, a queued one included (R2).
        refusal = self._authority_refusal(trigger, row)
        if refusal is not None:
            from app import automation_fire

            if row.api_key_id is None:
                # A refused API key is its delivery's fault, not the trigger's: other keys still file.
                trigger_health.mark_failed(self.session, trigger, refusal)
            if resuming:
                automation_fire.close_owed(self.session, row, refusal)
                if automation_fire.has_filed(self.session, row.id):
                    # Some targets filed before the refusal: the delivery stands as what it filed.
                    self.session.flush()
                    self.summary.count(automation_fire.finalize_delivery(self.session, row).state)
                    return
                self._release_reservation(row)
            self._finish(trigger, row, SKIPPED, refusal)
            return
        if resuming:
            self._file(trigger, row)
            return
        if trigger.preflight_kind != PREFLIGHT_NONE and row.parent_delivery_id is None and row.preflighted_at is None:
            self._claim_preflight(trigger, row)
            return
        self._admit(trigger, row)

    def _authority_refusal(self, trigger: TaskTrigger, row: TriggerDelivery) -> Optional[str]:
        """Why the filer may not file at all now, or None: the trigger's owner, or the owner of the API key `row`
        arrived with (`automation_fire.key_filer`). Re-asked at every filing (R2), the way a cron's firing asks
        it. A fan-out's per-project WRITE is the fire engine's, per target (`automation_fire.file_task_target`)."""
        from app import automation_fire
        from app.authz.catalog import permission_key
        from app.authz.principal import principal_for_user

        automation = self._automation(trigger)
        if row.api_key_id is not None:
            filer = automation_fire.key_filer(self.session, row.api_key_id, trigger.org_id)
            if isinstance(filer, str):
                return filer
            principal = filer.principal
            if automation.target_kind is None:
                return None
        else:
            owner = trigger_owner(self.session, trigger)
            if owner is None:
                return "the trigger's owner no longer exists — re-create the trigger as a current member"
            if owner.org_id is not None and owner.org_id != trigger.org_id:
                # A filing runs under its owner's licence and git access; it must not cross the tenancy boundary,
                # as a cron's firing must not (`automation_fire._owner_or_reason`).
                return "the trigger's owner is in another organization — re-create it as a member of this one"
            if automation.target_kind is None:
                # An action with no Target files no Task (Send email): the engine asks its own rule, an org admin
                # (`automation_fire._owner_or_reason`).
                return None
            principal = principal_for_user(self.session, owner)
        if not principal.has(permission_key("tasks", "create")):
            return REASON_OWNER_LACKS_CREATE
        if not principal.has(permission_key("tasks", "edit")):
            return "owner lacks tasks:edit"
        # A one-project trigger keeps today's up-front project check and sentence (before dedup, rate and any
        # paid preflight); a fan-out asks it per target instead, so one unreachable project never stops the rest.
        if (automation.target_kind == TARGET_PROJECTS and automation.target_mode == MODE_SPECIFIC
                and len(automation.target_ids or []) == 1):
            from app.authz.object_access import can_access
            from app.authz.visibility import ObjectAccess

            if not can_access(self.session, principal, "project", int(automation.target_ids[0]),
                              mode=ObjectAccess.WRITE):
                return "owner has no access to the trigger's project"
        return None

    def _finish(self, trigger: TaskTrigger, row: TriggerDelivery, state: str, reason: Optional[str], *,
                counted: bool = True) -> None:
        """Log `row`'s outcome, apply the counter rule, and commit."""
        now = datetime.now(timezone.utc)
        slack_thread = _slack_thread(trigger, row.event)
        row.state = state
        row.reason = reason
        row.processed_at = now
        row.claimed_until = None
        if state in _BODILESS_OUTCOMES:
            # Nothing will ever render this event again; the preview stays for the log. Bounds what a valid
            # sender can park in the database to the rows that file.
            row.event = None
        if counted:
            trigger_health.apply_outcome(trigger, state, reason=reason, now=now)
            trigger_health.auto_pause_if_due(self.session, trigger, now=now)
        self.summary.count(state)
        self.session.commit()
        if state != DEDUPED:
            # A duplicate is a redelivery of a mention already answered; every other end here gets one sentence.
            _reply_in_slack(slack_thread, _slack_reply_for(state))

    def _release_reservation(self, row: TriggerDelivery) -> None:
        if row.dedup_key:
            self.session.execute(
                delete(TriggerDedup).where(
                    TriggerDedup.trigger_id == self.trigger_id,
                    TriggerDedup.dedup_key == row.dedup_key,
                    TriggerDedup.delivery_id == row.id,
                )
            )

    def _admit(self, trigger: TaskTrigger, row: TriggerDelivery) -> None:
        """Steps 4-6: dedup, rate, concurrency/queue; then file."""
        session = self.session
        now = datetime.now(timezone.utc)
        if row.dedup_key:
            reserved = session.execute(
                pg_insert(TriggerDedup)
                .values(trigger_id=self.trigger_id, dedup_key=row.dedup_key, org_id=self.org, delivery_id=row.id)
                .on_conflict_do_nothing(index_elements=["trigger_id", "dedup_key"])
                .returning(TriggerDedup.dedup_key)
            ).first()
            if reserved is None:
                first = session.scalar(
                    select(TriggerDedup).where(
                        TriggerDedup.trigger_id == self.trigger_id, TriggerDedup.dedup_key == row.dedup_key
                    )
                )
                if first is not None and first.task_id is not None:
                    reason = f"duplicate of Task #{first.task_id}"
                    row.task_id = first.task_id
                elif first is not None and first.delivery_id is not None:
                    # A delivery that acted with no Task (a sent email) holds its key with no task id.
                    first_state = self.session.scalar(
                        select(TriggerDelivery.state).where(TriggerDelivery.id == first.delivery_id))
                    reason = (f"duplicate of delivery #{first.delivery_id}" if first_state == DISPATCHED
                              else f"duplicate of delivery #{first.delivery_id}, which is queued")
                else:
                    reason = "duplicate of an item already received"
                self._finish(trigger, row, DEDUPED, reason)
                return
        if filings_in_last_hour(session, self.trigger_id, now) >= trigger.rate_limit_per_hour:
            self._release_reservation(row)
            self._finish(trigger, row, RATE_CAPPED, f"Rate capped: {trigger.rate_limit_per_hour}/hour reached")
            return
        # The concurrency cap and the queue are per target: the fire engine queues a target at its cap (§4.3).
        self._file(trigger, row)

    def _automation(self, trigger: TaskTrigger):
        """The trigger's parent automation. Every current writer links one; a trigger with none (written by a
        release without automations and never delivered since) is adopted here, in this transaction
        (`automation_service.sync_trigger`, the backfill's own mapping)."""
        from app import automation_service
        from app.models.automations import Automation

        if trigger.automation_id is not None:
            automation = self.session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
            if automation is not None:
                return automation
        return automation_service.sync_trigger(self.session, trigger)

    def _file(self, trigger: TaskTrigger, row: TriggerDelivery) -> None:
        """Step 7: fire the trigger's automation for this delivery — one task per target, each in its own commit,
        the delivery's verdict and its dedup row in the last. A fault past the engine closes the delivery from its
        ledger: `dispatched` when any target filed (the reservation stands), else `failed` (counted, released)."""
        from app import automation_fire

        session = self.session
        row_id = row.id
        slack_thread = _slack_thread(trigger, row.event)
        try:
            result = file_task(session, trigger, row, self.org, deadline=self.summary.deadline)
        except Exception as exc:  # noqa: BLE001 — any filing fault is this delivery's failure, logged readably
            session.rollback()
            reason = f"filing failed: {readable_error(exc)}"
            log.warning("trigger %s delivery %s: %s", self.trigger_id, row_id, reason)
            trigger = self._lock()
            if trigger is None:
                session.rollback()
                return
            row = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == row_id))
            if row is None or (row.state == DISPATCHED and row.task_id is not None):
                # Gone, or the filing had already committed before the fault: the Task exists, so the delivery is
                # not a failure and its dedup reservation must stand.
                session.rollback()
                return
            if row.state == FILING or automation_fire.has_filed(session, row.id):
                # Targets may already have filed: the ledger decides, and a reservation stands if one did.
                if row.state == QUEUED:
                    automation_fire.close_owed(session, row, reason)
                result = automation_fire.finalize_delivery(session, row, error=reason)
                self.summary.count(result.state)
                self._reply_filed(slack_thread, row_id, result)
                return
            # Nothing filed: today's end — failed, counted, released (a queued row's owed targets say why).
            automation_fire.close_owed(session, row, reason)
            self._release_reservation(row)
            self._finish(trigger, row, FAILED, reason)
            return
        self.summary.count(result.state)
        if result.task_ids:
            log.info("trigger %s delivery %s: filed task(s) %s", self.trigger_id, row_id, result.task_ids)
        self._reply_filed(slack_thread, row_id, result)

    def _reply_filed(self, slack_thread: Optional[tuple[str, str, UUID]], row_id: int, result) -> None:
        """One threaded Slack sentence about what a firing came to: the filed tasks, or why nothing filed. A delivery
        still queued with nothing filed says nothing yet (it will, once it files)."""
        if slack_thread is None:
            return
        if not result.task_ids:
            if result.state == DISPATCHED:
                # It acted with no Task: an automation that sends an email.
                _reply_in_slack(slack_thread, "Sent the email.")
            elif result.state not in (QUEUED, DEDUPED):
                _reply_in_slack(slack_thread, _slack_reply_for(result.state))
            return
        from app.task_external_comment import _console_task_url

        try:
            links = ", ".join(f"<{_console_task_url(task_id)}|Task #{task_id}>" for task_id in result.task_ids)
            _reply_in_slack(slack_thread, f"Filed {links}.")
        except Exception:  # noqa: BLE001 — the Task is filed; a missing acknowledgement must not stop the pass
            log.exception("trigger %s delivery %s: could not queue the Slack reply", self.trigger_id, row_id)

    # ── preflight ──

    def _claim_preflight(self, trigger: TaskTrigger, row: TriggerDelivery) -> None:
        """Move `row` to `preflighting` under a fresh claim and hand it to the pool. A second row of the same
        trigger cannot be claimed (the partial unique index): it stays `received` for a later pass."""
        session = self.session
        claim = datetime.now(timezone.utc) + timedelta(seconds=trigger.preflight_timeout_seconds) + CLAIM_SLACK
        try:
            with session.begin_nested():
                session.execute(
                    update(TriggerDelivery)
                    .where(TriggerDelivery.id == row.id, TriggerDelivery.state == RECEIVED)
                    .values(state=PREFLIGHTING, claimed_until=claim)
                )
        except IntegrityError:
            self.skip.add(row.id)
            session.commit()
            return
        session.commit()
        self.skip.add(row.id)
        PREFLIGHT_POOL.submit(run_preflight, self.trigger_id, row.id, claim)


def filings_in_last_hour(session: Session, trigger_id: UUID, now: datetime) -> int:
    """The rate limit's count (§3.6 step 5): deliveries of this trigger that filed, or wait to, in the last hour —
    or that acted with no Target at all (a Send email, which writes no ledger row). Shared by the processor and an
    automation's Run now, so a Run now spends the same hourly budget."""

    untargeted = ~select(AutomationRunTarget.id).where(
        AutomationRunTarget.delivery_id == TriggerDelivery.id).exists()
    return session.scalar(
        select(func.count()).select_from(TriggerDelivery).where(
            TriggerDelivery.trigger_id == trigger_id,
            TriggerDelivery.processed_at >= now - timedelta(hours=1),
            or_(
                TriggerDelivery.state == QUEUED,
                (TriggerDelivery.state == DISPATCHED) & (TriggerDelivery.task_id.is_not(None) | untargeted),
            ),
        )
    ) or 0


def file_task(session: Session, trigger: TaskTrigger, row: TriggerDelivery, org: UUID, *,
              deadline: Optional[float] = None):
    """Fire the trigger's automation for `row` (`automation_fire.fire`, cause `event`; a queued or crashed delivery
    resumes its owed targets). Commits, per target. Returns the engine's `FireResult`. The projects are ALWAYS the
    automation's own Target (AC-2.4): nothing in the event can name another."""
    from app import automation_fire, automation_service
    from app.models.automations import Automation

    automation = None
    if trigger.automation_id is not None:
        automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
    if automation is None:
        automation = automation_service.sync_trigger(session, trigger)
    # The engine stops at its bound less its slack; aimed at the pass's own deadline when there is one.
    bound = None if deadline is None else (deadline - time.monotonic()) + automation_fire.DEADLINE_SLACK_SECONDS
    return automation_fire.fire(session, automation, cause=automation_fire.CAUSE_EVENT, delivery=row,
                                event=row.event or {}, timeout_seconds=bound)


def seen_keys(session: Session, trigger_id: UUID) -> list[str]:
    return list(
        session.scalars(
            select(TriggerDedup.dedup_key)
            .where(TriggerDedup.trigger_id == trigger_id)
            .order_by(TriggerDedup.first_seen_at.desc())
            .limit(SEEN_KEYS_LIMIT)
        )
    )


def run_preflight(trigger_id: UUID, delivery_id: int, claim: datetime) -> None:
    """The pool worker: build the input, call the trigger's preflight runner with no transaction held, apply the
    result under the claim, then fire the processor so steps 4-7 follow at once. Never raises."""
    outcome: PreflightResult | PreflightError | PreflightUnavailable
    try:
        with trigger_session(trigger_id) as (session, _org):
            trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id))
            row = session.scalar(
                select(TriggerDelivery).where(
                    TriggerDelivery.id == delivery_id,
                    TriggerDelivery.state == PREFLIGHTING,
                    TriggerDelivery.claimed_until == claim,
                )
            )
            if trigger is None or row is None:
                return
            preflight_input = PreflightInput(
                event=dict(row.event or {}), state=dict(trigger.state_document or {}),
                seen_keys=seen_keys(session, trigger_id),
            )
            session.commit()
            runner = PREFLIGHT_RUNNERS.get(trigger.preflight_kind)
            try:
                if runner is None:
                    raise PreflightError(f"{trigger.preflight_kind} preflight is not available on this control plane yet")
                outcome = runner(session, trigger, preflight_input)
            except (PreflightError, PreflightUnavailable) as exc:
                outcome = exc
            except Exception as exc:  # noqa: BLE001 — a runner bug is this delivery's failure, not the pool's
                outcome = PreflightError(readable_error(exc))
            session.rollback()
        apply_preflight_result(trigger_id, delivery_id, claim, outcome)
    except Exception:  # noqa: BLE001 — the claim expires and the row returns to the queue
        log.exception("trigger %s delivery %s: preflight worker failed", trigger_id, delivery_id)
        return
    if not isinstance(outcome, PreflightUnavailable):
        fire_intake()


def apply_preflight_result(
    trigger_id: UUID, delivery_id: int, claim: datetime,
    outcome: "PreflightResult | PreflightError | PreflightUnavailable", *, now: Optional[datetime] = None,
) -> bool:
    """Apply a preflight's outcome ONLY while the claim it ran under still stands — a compare-and-swap on
    `(id, state='preflighting', claimed_until=claim)`. A stale worker (its claim expired and the row was
    re-claimed or processed) writes NOTHING: no outcome, no state document. True when it applied."""
    now = now or datetime.now(timezone.utc)
    with trigger_session(trigger_id) as (session, org):
        trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id).with_for_update())
        row = session.scalar(
            select(TriggerDelivery)
            .where(
                TriggerDelivery.id == delivery_id,
                TriggerDelivery.state == PREFLIGHTING,
                TriggerDelivery.claimed_until == claim,
            )
            .with_for_update()
        )
        if trigger is None or row is None:
            session.rollback()
            return False
        row.claimed_until = None
        if isinstance(outcome, PreflightUnavailable):
            # Once preparing, the row keeps the longer window: the preflight right after the clone may still
            # meet a busy or briefly absent node. The reason carries the mark between attempts.
            preparing = isinstance(outcome, PreflightPreparing) or (row.reason or "").startswith(PREPARING_REASON)
            if now - row.received_at < (preparing_window() if preparing else UNAVAILABLE_WINDOW):
                row.state = RECEIVED
                row.attempts = (row.attempts or 0) + 1
                prefix = PREPARING_REASON if preparing else "preflight unavailable, retrying: "
                row.reason = f"{prefix}{readable_error(outcome)}"
                session.commit()
                return True
            outcome = PreflightError(readable_error(outcome))
        if isinstance(outcome, PreflightError):
            _finish_row(session, trigger, row, FAILED, f"preflight failed: {readable_error(outcome)}", now)
            session.commit()
            return True
        if outcome.new_state is not None:
            if len(json.dumps(outcome.new_state).encode()) > MAX_STATE_BYTES:
                _finish_row(session, trigger, row, FAILED, "preflight failed: state is over 64 KiB — the "
                            "previous state was kept", now)
                session.commit()
                return True
            trigger.state_document = outcome.new_state
        if outcome.items is not None:
            if not outcome.items:
                _finish_row(session, trigger, row, FILTERED, "preflight returned no items", now)
                session.commit()
                return True
            for item in outcome.items:
                item_event = {**(row.event or {}), "item": item}
                session.add(
                    TriggerDelivery(
                        trigger_id=trigger_id, org_id=org, parent_delivery_id=row.id, state=RECEIVED,
                        item_id=str(item["id"]), dedup_key=dedup_key_for(item["id"]), event=item_event,
                        preview=preview_of(item), preflighted_at=now, api_key_id=row.api_key_id,
                    )
                )
            _finish_row(session, trigger, row, DISPATCHED, f"preflight returned {len(outcome.items)} item(s), "
                        "each logged separately", now)
            session.commit()
            return True
        if outcome.run is False:
            _finish_row(session, trigger, row, FILTERED, f"preflight: {outcome.reason or 'nothing to do'}", now)
            session.commit()
            return True
        row.state = RECEIVED
        row.preflighted_at = now
        session.commit()
        return True


def _finish_row(session: Session, trigger: TaskTrigger, row: TriggerDelivery, state: str, reason: str,
                now: datetime) -> None:
    row.state = state
    row.reason = reason
    row.processed_at = now
    if state in _BODILESS_OUTCOMES:
        row.event = None
    trigger_health.apply_outcome(trigger, state, reason=reason, now=now)
    trigger_health.auto_pause_if_due(session, trigger, now=now)


# The preflight venues register themselves on import; last, because each imports this module's seam.
from app import trigger_preflight_model, trigger_preflight_script  # noqa: E402,F401
