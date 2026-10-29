"""User-scoped activity trail: records authenticated reads and writes.

`record_write` (the governance write-audit path) inserts `ActivityEvent` in the caller's own
transaction, the only path into the table: the audit row and the mutation land together or not at
all, and no network round trip is held across the caller's row locks.

`UserActivityMiddleware._record_read` (best-effort GET logging) runs its blocking dedup/insert on a
worker thread via a fire-and-forget task, so it never blocks the event loop or the response."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import unicodedata
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# `_text` is private and imported anyway: the one home for making a caller-controlled string storable.
from app import act_as, edition_features, request_caller
from app.activity_kinds import SENTENCE_TEMPLATES, SUBJECT_PARAMS, _text
from app.actors import transition_actor
from app.credentials.registry import CREDENTIAL_TYPES
from app.entity_shape import optional_uuid
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.tasks import Task
from app.tenancy import exclusion_log, seam
from app.tenancy.ids import ORG_ZERO_ID

if TYPE_CHECKING:
    from app.support_agent import assist

log = logging.getLogger("app.user_activity")

READ_EXCLUDED_EXACT = {"/api/auth/me", "/api/health"}
# `/api/audit` is the audit browse page: without it, reading the trail writes self-referential rows
# (about one per reader per minute; `_recently_recorded` ignores the query), and AC5.4 requires none.
# Cost: "who read the audit log" is not answerable here (an operator follow-up). A prefix, so any later
# route under it is silently exempt too; choose deliberately. `/api/activity` is its predecessor.
# `/api/git-connect`, `/api/slack-connect`, `/api/email-connect`: the OAuth callback's query carries a live `code` and
# `state`.
READ_EXCLUDED_PREFIXES = (
    "/api/auth/", "/api/public/", "/api/activity", "/api/audit", "/api/git-connect", "/api/slack-connect",
    "/api/email-connect",
)
# The Audit export tab polls its destination every minute while open: each poll would write a read row the export
# then reports as waiting to send, the same self-reference as `/api/audit`.
READ_EXCLUDED_PATTERNS = (re.compile(r"/api/orgs/[^/]+/siem-export"),)
FILTER_QUERY_KEYS = {
    "q", "search", "query", "filter", "action_type", "kind", "status",
    "start", "end", "user_id", "type",
}
PAGINATION_QUERY_KEYS = {"limit", "offset", "page", "cursor"}
READ_DEDUPE_WINDOW = timedelta(seconds=60)
#: `data.assisted_by` of a read the support agent made for the user (`app/support_agent/assist.py`).
ASSISTED_BY = "jarvis"

# Strong references for fire-and-forget tasks until done (the standard asyncio idiom).
_background_tasks: set[asyncio.Task] = set()


def _read_excluded(path: str) -> bool:
    if path in READ_EXCLUDED_EXACT:
        return True
    if any(path.startswith(p) for p in READ_EXCLUDED_PREFIXES):
        return True
    return any(p.fullmatch(path) for p in READ_EXCLUDED_PATTERNS)


# `(user, path, assisted)` → when its last read row was written, as far as THIS process knows. A hit inside
# the window answers `_recently_recorded`'s question without a session — after every console GET that was a
# session, a tenancy bind and two SELECTs (~2% of the api's CPU). Only ever an answer the DB would
# give too: a miss or an expired entry falls through to the DB read below. Times are the DB's own (`now()`,
# which is the row's `created_at`), and the memo's window is `_MEMO_MARGIN` shorter than the rule's, so app/DB
# clock skew can only send a read to the DB early, never suppress one the DB would record.
_recent_reads: dict[tuple[UUID, str, bool], datetime] = {}
_MEMO_MARGIN = timedelta(seconds=5)
_recent_reads_lock = threading.Lock()
_RECENT_READS_MAX = 10_000


def _remember_read(user_id: UUID, path: str, assisted: bool, at: datetime) -> None:
    with _recent_reads_lock:
        if len(_recent_reads) >= _RECENT_READS_MAX:
            _recent_reads.clear()  # a bound, not a policy: the next reads just ask the DB again
        _recent_reads[(user_id, path, assisted)] = at


def _known_recent_read(user_id: UUID, path: str, assisted: bool) -> bool:
    at = _recent_reads.get((user_id, path, assisted))
    return at is not None and at >= datetime.now(timezone.utc) - (READ_DEDUPE_WINDOW - _MEMO_MARGIN)


def _recently_recorded(session: Session, user_id: UUID, path: str, *, assisted: bool = False) -> bool:
    """A read of `path` by `user_id` already recorded inside the window — a Jarvis-assisted read and the
    user's own are deduplicated apart, so neither hides the other."""
    cutoff = datetime.now(timezone.utc) - READ_DEDUPE_WINDOW
    by_jarvis = ActivityEvent.data["assisted_by"].astext == ASSISTED_BY
    last = session.scalar(
        select(ActivityEvent.created_at)
        .where(
            ActivityEvent.user_id == user_id,
            ActivityEvent.action_type == "read",
            ActivityEvent.path == path,
            by_jarvis if assisted else by_jarvis.is_not(True),
        )
        .order_by(ActivityEvent.created_at.desc())
        .limit(1)
    )
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    _remember_read(user_id, path, assisted, last)
    return last >= cutoff


class UserActivityMiddleware(BaseHTTPMiddleware):
    """The innermost of the app's three middlewares, and every property below follows from that.

    `Starlette.add_middleware` inserts at 0, so `app/main.py` yields SecurityHeaders -> SessionAuth ->
    UserActivity -> router: `request.state.admin_id` is set inbound, a SessionAuth 401 is never recorded,
    and the three public auth routes still reach us.

    `dispatch` branches on method first, so a read pays nothing for the write branch."""

    async def dispatch(self, request: Request, call_next):
        if request.method in WRITE_METHODS and not _write_excluded(request.url.path):
            return await self._dispatch_write(request, call_next)
        response = await call_next(request)
        try:
            if request.method == "GET" and response.status_code < 400:
                admin_id = getattr(request.state, "admin_id", None)
                path = request.url.path
                if admin_id is not None and not _read_excluded(path):
                    # Read now, not on the worker: the turn may have released its mark by then.
                    by = None
                    if edition_features.enabled("support_assistant"):
                        from app.support_agent import assist

                        by = assist.of(request)
                    self._schedule_record_read(admin_id, request, by)
        except Exception:
            log.exception("read-activity capture failed")
        return response

    async def _dispatch_write(self, request: Request, call_next):
        """The write branch (R1); every step's position is load-bearing (see `_WriteCapture`).

        Best-effort like the read path: capture failures are logged and swallowed (AC1.4). The downstream
        exception itself is re-raised so a real 500 still reaches the client."""
        capture = _WriteCapture(request)
        try:
            await capture.read_request()
        except Exception:
            log.exception("write-activity request capture failed")
        try:
            # Bound around `call_next`, whose task copies this context: every row the endpoint writes names its
            # caller (`app/request_caller.py`).
            with request_caller.bound(request, capture.caller):
                response = await call_next(request)
        except asyncio.CancelledError:
            # Exit (d), cancellation: nothing is sent, so recording 500 would be false; marked disconnected
            # instead.
            capture.client_disconnected = True
            capture.schedule_once()
            raise
        except BaseException:
            # Exit (d): `call_next` raised, so there is no tee. 500 is what `ServerErrorMiddleware` will send.
            capture.schedule_once(status=500)
            raise
        try:
            capture.note_response(response)
            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is None:
                # Not a streaming response: nothing to tee, so the row is due now (a guard; starlette always streams).
                capture.schedule_once()
            else:
                response.body_iterator = capture.tee(body_iterator)
        except Exception:
            log.exception("write-activity capture failed")
        return response

    def _schedule_record_read(self, admin_id: UUID, request: Request, by: Optional[assist.Assist] = None) -> None:
        # Blocking DB work moved to a worker thread, fire-and-forget, so neither the loop nor this response
        # waits on it.
        task = asyncio.create_task(asyncio.to_thread(self._record_read, admin_id, request, by))
        _background_tasks.add(task)
        task.add_done_callback(_on_record_read_done)

    def _record_read(self, admin_id: UUID, request: Request, by: Optional[assist.Assist] = None) -> None:
        """One read row, deduplicated per path per minute. `by` marks it as a read the support agent made for
        the user: the sentence ends "(via Jarvis)" and `data` names the chat."""
        from app.db import SessionLocal

        params = dict(request.query_params)
        filter_params = {k: v for k, v in params.items() if k not in PAGINATION_QUERY_KEYS}
        is_filter = any(k in FILTER_QUERY_KEYS for k in filter_params)
        path = request.url.path
        if _known_recent_read(admin_id, path, by is not None):
            return
        # Attributed: this runs on a worker after the response, where the request's label is invisible.
        with exclusion_log.caller("middleware:user_activity.read"), SessionLocal() as session:
            # Bound from the reader's own row here: `BaseHTTPMiddleware` runs the app in a child task, so the
            # guard's `seam.bind()` never reaches this thread. Not `operator_scope()`: a user's read trail would
            # become tenancy-free (dedup matching any org's row). A missing admin row writes nothing, as
            # `session.get` below already did; `TenancyRowMissing` is made to agree with that.
            try:
                with seam.bind_from(session, AdminUser, admin_id):
                    if _recently_recorded(session, admin_id, path, assisted=by is not None):
                        return
                    user = session.get(AdminUser, admin_id)
                    if user is None:
                        return
                    kind = "filter_applied" if is_filter else "page_view"
                    verb = "Filtered" if is_filter else "Opened"
                    data: dict[str, Any] = {"params": params} if params else {}
                    summary = f"{verb} {path}"
                    if by is not None:
                        data.update(assisted_by=ASSISTED_BY, support_conversation_id=by.conversation_id)
                        summary += " (via Jarvis)"
                    record(
                        session, user, "read", kind, summary,
                        path=path, method=request.method, data=data or None,
                    )
                    # `now()` is the transaction's start: exactly the `created_at` the row is written with.
                    written_at = session.scalar(select(func.now()))
                    session.commit()
                    _remember_read(admin_id, path, by is not None, written_at)
            except seam.TenancyRowMissing:
                return


def _on_capture_done(task: asyncio.Task) -> None:
    """The write branch's twin of `_on_record_read_done`, existing only for its message: a lost write
    audit row must be distinguishable in the logs from read-path noise."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("write-activity capture failed — an audit row was LOST", exc_info=exc)


def _on_record_read_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("read-activity capture failed", exc_info=exc)


def record(
    session: Session,
    user: Optional[AdminUser],
    action_type: str,
    kind: str,
    summary: str,
    *,
    path: Optional[str] = None,
    method: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
) -> None:
    try:
        session.add(
            ActivityEvent(
                user_id=user.id if user is not None else None,
                actor_label=user.email if user is not None else None,
                action_type=action_type,
                kind=kind,
                summary=summary,
                path=path,
                method=method,
                data=data,
                # The actor's org, named rather than left to the seam (as in `record_write`): `activity_events` is
                # `Stored` (`app/tenancy/manifest.py`), so the seam has only the bound scope. `_record_read` binds the
                # actor (identical value), but a caller on the operator plane stamps nothing, and with `org_id` NOT NULL
                # that would fail its commit. A column default is forbidden
                # (`test_no_create_path_falls_back_to_a_well_known_org`).
                org_id=user.org_id if user is not None else None,
            )
        )
    except Exception:
        log.exception("activity record failed (%s, %s)", action_type, kind)


def record_task_transition(
    session: Session, task_id: Any, action: str, extra: Optional[dict[str, Any]] = None,
    org_id: Optional[UUID] = None,
) -> None:
    """Persist one durable ``ActivityEvent`` row for a task lifecycle transition (E3, issue #975).

    Called only from the ``entity == "task"`` branch of ``emit_console_event`` (app/events.py), on the
    same ``session`` as the transition and its ``pg_notify``, so it commits atomically with it. (Its
    duplication of ``record_write``'s construction is an open design question, not a requirement.)

    The shared read contract for ``_epic_time_metrics`` and ``get_task_activity`` (app/routes/tasks/):
    ``kind LIKE 'task:%'`` rows ordered by ``created_at``, keyed by ``data->>'id'``, carrying the task's
    resulting ``state`` as the session sees it (``None`` when not visible). ``extra`` merges row-only
    detail into ``data``; a reserved key (``entity``/``id``/``state``/``actor_id``) in it is dropped so it can
    never shadow the base values. No ``summary`` (see the write below).

    ``data.actor_id`` is the person who caused the transition (`actors.transition_actor`), so the Activity
    feed can name them; absent for a machine act. Not ``user_id``: the audit trail files ``task:%`` rows as
    machine rows and attributes the act to its captured request row instead (D17, ``routes/audit.py``).
    """
    task = session.get(Task, task_id)
    state = task.state if task is not None else None
    # kind is String(48); every real action is well under that — slice is a defensive guard only.
    kind = f"task:{action}"[:48]
    data: dict[str, Any] = {"entity": "task", "id": task_id, "state": state}
    actor_id = transition_actor(session)
    if actor_id is not None:
        data["actor_id"] = str(actor_id)
    if extra:
        data.update({k: v for k, v in extra.items() if k not in data})
    # Who pressed it, beyond `actor_id`: the browser, address and console session.
    row_org = getattr(task, "org_id", None) if task is not None else org_id
    data = request_caller.merged(data, row_org)
    session.add(
        ActivityEvent(
            action_type="write",
            kind=kind,
            # Deliberately empty: `kind` and `data` already hold what the feed's `_transition_label` renders.
            # Empty rather than omitted (NOT NULL); `record_task_note` carries real summaries.
            summary="",
            data=data,
            # The task's org, not the caller's: `activity_events` is `Stored`, so on an operator-plane route
            # (which stamps nothing) `_refuses_unstamped_create` would refuse and fail the whole sweep. An org-bound
            # caller can only see tasks in its own org, so this equals its bound scope. `getattr` for
            # `tests/test_events.py`'s stub task. When the task is no longer visible (the `deleted` transition, after
            # `session.delete`), the caller-supplied `org_id` (read before the delete by `delete_task_core`) is used.
            org_id=row_org,
        )
    )


def record_task_note(
    session: Session,
    task_id: Any,
    action: str,
    summary: str,
    extra: dict[str, Any],
    *,
    user: Optional[AdminUser] = None,
) -> None:
    """Persist one durable ``ActivityEvent`` row carrying richer per-task content (a Q&A resolution, a
    stage output) beyond `record_task_transition`'s bare snapshot.

    Same shape as that sibling: inserts on the caller's session and does not commit, keyed by
    ``data->>'id'``, with ``extra`` merged into ``data``; the Q&A builder scans ``task_note:answered``.
    The ``task_note:`` namespace is crucial: a state-less row in the ``kind LIKE 'task:%'`` stream would
    corrupt interval readers (``task_note:x`` never matches, the ``:`` is literal).

    ``user`` (R4.9) is keyword-only with a default so existing positional call sites are unchanged; when
    passed it stamps ``user_id``/``actor_label`` so an `answer` resolving a security gate is attributable."""
    kind = f"task_note:{action}"[:48]
    # Resolved for its org exactly as `record_task_transition` does, so one task's trail is never
    # split across orgs. Usually free: callers hold the Task in the identity map. A scalar subquery is not
    # possible here: `write_seam._apply` compares the mapped instance's `org_id` with `!=`, which on a
    # `ScalarSelect` raises.
    task = session.get(Task, task_id)
    session.add(
        ActivityEvent(
            user_id=user.id if user is not None else None,
            actor_label=user.email if user is not None else None,
            action_type="write",
            kind=kind,
            summary=summary,
            data={"entity": "task", "id": task_id, **extra},
            # The task's org, not the actor's, and not the seam's to supply: `activity_events` is `Stored`, and
            # machinery callers (`lease_drain`, agent-plane settles) have no bind, so with `org_id` NOT NULL they
            # would 500; a column default is ruled out (`test_no_create_path_falls_back_to_a_well_known_org`).
            # The row is about a tenant object, so it files under that object's org. `getattr` so a stub Task does
            # not raise; `None` leaves the disposition to the bound scope or a refusal.
            org_id=getattr(task, "org_id", None),
        )
    )


def _uuids_to_str(data: dict[str, Any]) -> dict[str, Any]:
    """Replace `uuid.UUID` values with their string form, one level deep plus inside list values.

    At the one home rather than ~18 call sites: a raw `UUID` in JSONB raises at flush, and for
    `record_write` that rolls back the audited mutation. Covers every shape this tree writes (scalars,
    scalar lists, one level of dict inside a list or dict); deliberately no deeper. Stored ids become
    uuid strings."""
    def _scalar(value: Any) -> Any:
        return str(value) if isinstance(value, UUID) else value

    def _one_level(value: Any) -> Any:
        # Both containers by the same rule: `{"k": [{"id": UUID}]}` and `{"k": {"id": UUID}}` are both live
        # (`routes/groups.py`, `routes/projects.py`'s `jira_credential_change`). Still exactly one level deep.
        if isinstance(value, dict):
            return {k: _scalar(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_scalar(v) if not isinstance(v, dict) else
                    {k: _scalar(x) for k, x in v.items()} for v in value]
        return _scalar(value)

    return {key: _one_level(value) for key, value in data.items()}


def record_write(
    session: Session,
    user: Optional[AdminUser],
    kind: str,
    summary: str,
    *,
    data: Optional[dict[str, Any]] = None,
    org_id: Optional[UUID] = None,
    in_org: Optional[UUID] = None,
) -> None:
    # `in_org` files the row in that org even with an actor — for a write that happened in an org other than
    # the actor's own (an operator's org transfer files one row in each org it touched, `app/tenancy/transfer.py`).
    # Load-bearing, unlike `record`: if staging fails the exception propagates and rolls the mutation
    # back, so it never commits unaudited. The audit never lands ahead of the caller's commit, and no
    # network call runs while the caller holds row locks. `org_id` is named (the actor's), not left to the write seam: on the
    # operator plane (`seam.OPERATOR`, `app/authz/enforce.py::_bind_read_scope`) an org-less create is refused, which
    # would 500 every operator-plane write (`test_crons_routes.py::test_every_successful_write_reconciles`).
    # An actorless row names its org (`org_id`, e.g. a refused sign-in's user's): `activity_events.org_id`
    # is NOT NULL, so an unnamed one lands only where a bound scope stamps it.
    operator_org = act_as.operator_org(user)
    if user is not None and operator_org is not None and user.org_id is not None:
        # An operator acting as an org (`act_as.seat_user`): filed in that org, naming the operator's own too.
        data = {**(data or {}), "acting_as": act_as.audit_data(user.org_id, operator_org)}
    event = ActivityEvent(
        user_id=user.id if user is not None else None,
        actor_label=user.email if user is not None else None,
        action_type="write",
        kind=kind,
        summary=summary,
        data=_uuids_to_str(data) if data is not None else None,
        # The actor's org here, the object's org in `record_task_transition`: this function is generic and
        # usually holds no object. Cost: a superadmin in org A acting on org B's object files the row under A
        # (unchanged from before the flip). The intended end state is the object's org everywhere, a named
        # follow-up across every audited route.
        org_id=in_org if in_org is not None else (user.org_id if user is not None else org_id),
    )
    session.add(event)
    # A write about a customer is also that customer's history, filed under IT (`app/customer_history.py`),
    # in the same transaction and just as load-bearing. The community edition has no customers.
    if edition_features.enabled("customers"):
        from app import customer_history

        customer_history.from_activity(session, event, user, in_org)


# ── Body capture: redaction and the size cap (R3/AC3.2) ───────────────────────────────────────────
# The write-capture branch calls both: `redact` on params/query, `serialise_body` on each body. Pure
# and unit-tested independently.

REDACTED = "<redacted>"

# The whole normalised key, or any contiguous run of its segments, for compounds whose segments are
# individually innocent (`access_key`). The run test catches `X-Api-Key`, `openai_api_key`,
# `api_key_id`, and cannot fire on `keywords` (one segment).
FULL_TERMS: frozenset[str] = frozenset({
    "api_key", "apikey", "access_key", "secret_key", "private_key", "client_secret",
    # A Gmail service-account key upload (`routes/org_email.py`), second to its body suppression.
    "key_json",
    # Run-together spellings: the all-lower `secretkey` is one segment that matched nothing.
    "secretkey", "privatekey", "clientsecret", "accesskey",
    # A rendered command is a secret carrier: `onboarding_command` is a `curl` with the node's plaintext
    # token, and a teed 201 body is served at `system:view`. `bootstrap_script` is kept as a guard though
    # no route returns it now. Named in full: a bare `command` segment would blank `command_type` etc.
    "bootstrap_script", "onboarding_command",
    # A presigned URL is the same carrier: `upload_url` (`routes/attachments.py::create_attachment_ticket`,
    # a 201 on `/api/tasks/`, not excluded) carries the CP's access key id, STS token and a write signature.
    # `url` cannot be a segment term (it would blank every link), so it is named in full.
    "upload_url", "uploadurl",
    # Run-together spellings, for the same reason as the five above.
    "bootstrapscript", "onboardingcommand",
    # The second factor (`app/mfa.py`): a recovery code is a password substitute, shown once, and the
    # enrolment URI carries the TOTP secret.
    "recovery_code", "recovery_codes", "recoverycode", "recoverycodes", "otpauth_uri", "otpauthuri",
})

# The longest `FULL_TERMS` entry in segments; bounds the run scan below (unbounded, a 32 KB body cost
# hours of CPU). Derived from the set, never a literal, so a longer term added later is not silently
# inert; pinned by `test_activity_redaction.py`.
_MAX_TERM_SEGMENTS = max(term.count("_") + 1 for term in FULL_TERMS)

# Any single segment of the normalised key (`node_token`, `X-Authorization`, `db.password`).
SEGMENT_TERMS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "authorization", "passphrase", "cookie",
    # The plurals are different segments.
    "secrets", "passwords", "cookies",
})

# `tokens` is deliberately absent: it would blank `tokens_in`/`tokens_out`. Cost: a hypothetical
# `{"tokens": ["ghp_..."]}` stores in clear (no declared write-body field has that name).

# The credential registry's own classification, read not re-typed: it covers keys no spelling rule
# reaches (`client_id`, `access_key_id`), and a secret field added there is closed here for free.
_REGISTRY_SECRET_KEYS: frozenset[str] = frozenset(
    field.key
    for spec in CREDENTIAL_TYPES.values()
    for field in spec.fields
    if field.kind == "secret"
)

# The `value` half of a `{name|key, value}` pair: an innocent key holding a secret.
_PAIR_NAME_KEYS = ("name", "key")
# `literal` is the value column of `routes/project_env.py`'s catalogue, whose request/response bodies
# this middleware captures verbatim (at `audit:view`, wider than the route's own grant). Fires only on
# a secret-shaped sibling name; not a value heuristic. Residual: a refused (422) write's request body is
# still captured, so a secret literal under an innocuous name is stored in clear.
_PAIR_VALUE_KEYS = ("value", "literal")

# Both camelCase boundaries: the second splits an acronym run (`AWSSecretKey` -> `AWS` +
# `SecretKey`), else `secret` is swallowed. Residual: a key with no case boundary (`XAPIKEY`) is one
# segment and still missed.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Over it, a marker plus a redacted preview is stored rather than dropping the body.
BODY_MAX_BYTES = 8 * 1024

# The capture middleware's read ceiling, restated where it is enforced.
INPUT_MAX_BYTES = 32 * 1024

# Python recurses on the C stack, so an unbounded walk makes a small nested body a `RecursionError`
# that rolls back the audited mutation (via `record_write`). Nothing real is near 32 deep.
MAX_DEPTH = 32
TOO_DEEP = "<too-deep>"


def _normalise_key(key: str) -> list[str]:
    """`apiKey`, `api-key`, `API_KEY` and `db.password` as their lower-case segments.

    Segment equality, not substring: `token` as a substring would blank `tokens_in`, and `key` would
    blank `dedup_key`. Split on every non-alphanumeric run (`db.password`, `credential[token]`,
    `X-Authorization`); `casefold` after NFKC."""
    folded = unicodedata.normalize("NFKC", _CAMEL_BOUNDARY.sub("_", key)).casefold()
    return [segment for segment in _NON_ALNUM.split(folded) if segment]


def _is_secret_key(key: Any) -> bool:
    """True when `key` names a value that must never be stored.

    Three arms over the normalised segments: any single segment in `SEGMENT_TERMS`; any contiguous run
    of segments in `FULL_TERMS`; or the whole key in the credential registry's own secret set."""
    if not isinstance(key, str):
        return False
    segments = _normalise_key(key)
    if not segments:
        return False
    if any(segment in SEGMENT_TERMS for segment in segments):
        return True
    if "_".join(segments) in _REGISTRY_SECRET_KEYS:
        return True
    for start in range(len(segments)):
        stop = min(start + _MAX_TERM_SEGMENTS, len(segments))
        for end in range(start + 1, stop + 1):
            if "_".join(segments[start:end]) in FULL_TERMS:
                return True
    return False


def _secret_pair_value_keys(mapping: dict[Any, Any]) -> set[Any]:
    """The keys of `mapping` whose value is secret by virtue of a sibling: the
    `{"key": "GITHUB_TOKEN", "value": "ghp_..."}` shape no key-name rule can see.

    Matched over normalised names (so `Name`/`Value` count), and every value spelling is checked, not
    just the first present."""
    by_name: dict[str, Any] = {}
    for key in mapping:
        if isinstance(key, str):
            by_name.setdefault("_".join(_normalise_key(key)), key)
    value_keys = {
        by_name[spelling] for spelling in _PAIR_VALUE_KEYS if by_name.get(spelling) is not None
    }
    if not value_keys:
        return set()
    for name in _PAIR_NAME_KEYS:
        name_key = by_name.get(name)
        if name_key is not None and _is_secret_key(mapping.get(name_key)):
            return value_keys
    return set()


def redact(value: Any, *, _depth: int = 0) -> Any:
    """`value` with every secret-keyed entry replaced by `<redacted>`, at any nesting depth.

    Pure: returns a new structure. Walks dicts, lists and tuples to `MAX_DEPTH` (see that constant). A
    secret key's whole value is replaced whatever its shape, so nested key names are not disclosed.
    """
    if _depth >= MAX_DEPTH:
        return TOO_DEEP
    if isinstance(value, dict):
        pair_values = _secret_pair_value_keys(value)
        return {
            key: (
                REDACTED
                if _is_secret_key(key) or key in pair_values
                else redact(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


def _unparsed(size: int) -> dict[str, Any]:
    """The one marker for "nothing storable here": no `preview` key, ever."""
    return {"_truncated": True, "bytes": size, "unparsed": True}


def _reject_json_constant(constant: str) -> Any:
    """`NaN`/`Infinity`/`-Infinity` are legal to Python's `json` and illegal to Postgres `jsonb`.

    Left in, the INSERT errors and (via `record_write` staging on the caller's transaction) rolls back
    the audited mutation: a write-blocking DoS. Refused, so the body is recorded as unparseable."""
    raise ValueError(f"non-JSON constant in body: {constant}")


def _contains_nul(value: Any) -> bool:
    """True when any string in `value` (key or value, any depth) holds a NUL.

    Walks the structure, not the serialised text: `json.dumps` escapes NUL to a six-character sequence a
    legitimate body can also contain, so a substring test misfires. Bounded by `redact`'s depth cap."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    return False


def serialise_body(raw: bytes | str | None, *, complete: bool = True) -> Any:
    """The one rule for turning a captured request/response body into what `ActivityEvent.data` stores.

    `complete` is False when the capture's read ceiling cut the body short. Four outcomes only:
      * complete, parses, redacted text <= `BODY_MAX_BYTES` -> the redacted object itself;
      * complete, parses, larger -> `{"_truncated": True, "bytes": n, "preview": <first
        BODY_MAX_BYTES bytes of the redacted text>}`;
      * cut short, over `INPUT_MAX_BYTES`, or unparseable -> `{"_truncated": True, "bytes": n,
        "unparsed": True}` with no `preview`: un-redactable text is never stored;
      * nothing to store (`None`, empty, whitespace) -> `None`.

    A non-dict parsed body is returned as-is; the middleware wraps it (`_stored_body`). `bytes` is the
    original size. `_truncated` means "this is a marker, not the body".
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            payload = raw.encode()
        except UnicodeEncodeError:
            # Lone surrogates: not storable, but the size is knowable, so no fabricated `bytes: 0`.
            return _unparsed(len(raw.encode(errors="replace")))
    else:
        payload = raw
    size = len(payload)
    if not payload.strip():
        return None
    if not complete or size > INPUT_MAX_BYTES:
        return _unparsed(size)
    try:
        parsed = json.loads(payload, parse_constant=_reject_json_constant)
        redacted = redact(parsed)
        # The two remaining values Postgres cannot store, refused here since an INSERT error rolls back the
        # audited mutation: a NUL (illegal in `jsonb` and `text`), and a lone UTF-16 surrogate, which
        # `ensure_ascii=True` hides, hence forcing the UTF-8 encode.
        json.dumps(redacted, allow_nan=False, ensure_ascii=False).encode()
        if _contains_nul(redacted):
            return _unparsed(size)
        text = json.dumps(redacted, allow_nan=False)
    except (ValueError, RecursionError):
        # `ValueError` covers the decode errors and the refusals above; `RecursionError` (deep nesting in
        # `json.loads`) is not one and must be named or it 500s the mutation.
        return _unparsed(size)
    encoded = text.encode()
    if len(encoded) <= BODY_MAX_BYTES:
        return json.loads(text)
    # Clipped by bytes (a storage budget); `errors="ignore"` drops a split character.
    return {
        "_truncated": True,
        "bytes": size,
        "preview": encoded[:BODY_MAX_BYTES].decode(errors="ignore"),
    }


# ── Write capture: the middleware's second branch (R1/R2/R3) ──────────────────────────────────────

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The prefixes the write branch never records:
#   * `/api/agent/` (D3, belt to "no principal, no row"); `/api/public/` (no principal);
#   * `/api/console/` (the console plane); `/api/audit` (defence in depth only: GET-only today);
#   * `/api/git-connect`: its `POST .../start` response carries the App's `client_id`, a `secret` field.
#   * `/api/slack-connect`: likewise (the install URL); the callback records its own `slack_connected` row.
#   * `/api/operator/transfers`: a move writes its own `org_transfer` row in EACH org it touched (AC13), and a
#     plan is a read sent as a POST; a capture row in the operator's org would show the move twice.
# WS/SSE surfaces are absent: a method-keyed branch cannot reach GET/websocket routes.
WRITE_EXCLUDED_PREFIXES = (
    "/api/agent/", "/api/public/", "/api/console/", "/api/audit", "/api/git-connect", "/api/slack-connect",
    "/api/email-connect", "/api/operator/transfers",
)

# Exact paths the write branch never records. `/api/auth/switch` (an org switch): no audit event for a switch is
# the operator's decision (PRD §5 of the multi-org epic, Q8) — do not add one.
WRITE_EXCLUDED_EXACT = frozenset({"/api/auth/switch"})

# The public auth routes (`app/auth.py::PUBLIC_PATHS`), captured including failed sign-ins (D18): the
# one place a row has no `request.state.admin_id`. The two second-factor routes are where a sign-in now
# completes (`routes/mfa.py`); the login's own row is the first factor, answered 202 with no actor.
AUTH_WRITE_PATHS = frozenset({
    "/api/auth/login", "/api/auth/google", "/api/auth/logout",
    "/api/auth/mfa/verify", "/api/auth/mfa/enrol/confirm",
})
# The second-factor route whose success nests the `AdminOut` under `user` (beside the recovery codes).
_NESTED_USER_PATHS = frozenset({"/api/auth/mfa/enrol/confirm"})

# D20's backstop: a key-name denylist is fail-open, so on the secrets surface neither body is stored,
# only top-level key names. Git Access per forge (`routes/git_connections.py`) is the same surface: its
# PAT POST carries a live token.
CREDENTIAL_BODY_PREFIX = "/api/credentials"
GIT_CONNECTIONS_SEGMENT = "/git-connections"
# An org's Gmail connect (`routes/org_email.py`): the service-account POST carries the whole JSON key as a string,
# whose inner `private_key` no key-name redaction reaches; the handler-named row keeps the path alone.
EMAIL_CONNECTIONS_SEGMENT = "/email/connections"
# NodeVault's setup POST carries a key wrapped to every node — ciphertext, but no wrapped-key blob may reach an
# activity row; its routes audit what they decided themselves (`routes/nodevault.py::_audit`).
NODEVAULT_BODY_PREFIX = "/api/nodevault"
# One prospect's lookup and erase (`routes/prospect_data.py`): the email and what is held about them are the very data
# an erase removes, so no activity row may keep a copy; the erase audits its counts itself.
PROSPECT_DATA_BODY_PREFIX = "/api/support/prospect-data"
# An account export answers, and an import posts, an API key sealed under a typed passphrase
# (`app/account_transfer.py`): a stored copy is an offline guess away from the key. The export's own
# `credential_read` row records what was handed over.
ACCOUNT_TRANSFER_PATH = re.compile(r"^/api/accounts/(?:import|[^/]+/export)$")


# Jaah API keys (`routes/api_keys.py`): create and rotate answer the secret once. Each write records its own row
# (`api_key_created`, `…_edited` with the changed fields, `…_rotated`, `…_revoked`), never the secret.
API_KEYS_BODY_PREFIX = "/api/api-keys"


def _suppress_bodies(path: str) -> bool:
    return path.startswith(
        (CREDENTIAL_BODY_PREFIX, NODEVAULT_BODY_PREFIX, PROSPECT_DATA_BODY_PREFIX, API_KEYS_BODY_PREFIX)
    ) or (
        path.startswith("/api/users/") and GIT_CONNECTIONS_SEGMENT in path
    ) or (
        path.startswith("/api/orgs/") and EMAIL_CONNECTIONS_SEGMENT in path
    ) or ACCOUNT_TRANSFER_PATH.match(path) is not None

# Per-path response keys the denylist cannot name without blanking them everywhere: a Telegram link code is a
# bearer secret until it is redeemed (its deep link carries it too), but `code` and `url` are ordinary keys on
# every other route. The link row keeps only a hash (`telegram_link`); the audit row keeps `expires_at`.
RESPONSE_SECRET_KEYS: dict[str, frozenset[str]] = {
    "/api/auth/me/telegram-link-code": frozenset({"code", "url"}),
}


# What a refusal keeps of a FastAPI validation item: the field and the message, never `input`/`ctx`, which echo the
# submitted value whatever its key was.
_VALIDATION_ITEM_KEYS = ("loc", "msg")
# What a refusal keeps of an object `detail` (`{"code": ..., "message": ...}`, as `routes/projects.py` answers).
_DETAIL_OBJECT_KEYS = ("code", "message")
# A secret shorter than this is not scrubbed out of a refusal's text: `"1"` would blank every digit.
_MIN_SCRUBBED_SECRET = 4
# A detail string sharing a run this long with a secret is blanked whole: an echo may be truncated or re-quoted.
_SECRET_OVERLAP = 8


def _secret_strings(value: Any, *, _depth: int = 0) -> set[str]:
    """Every string `redact` would blank in `value`: the leaves under a secret key or a secret pair's value."""
    found: set[str] = set()
    if _depth >= MAX_DEPTH:
        return found
    if isinstance(value, dict):
        pair_values = _secret_pair_value_keys(value)
        for key, item in value.items():
            if _is_secret_key(key) or key in pair_values:
                found |= _all_strings(item, _depth=_depth + 1)
            else:
                found |= _secret_strings(item, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            found |= _secret_strings(item, _depth=_depth + 1)
    return found


def _all_strings(value: Any, *, _depth: int = 0) -> set[str]:
    if _depth >= MAX_DEPTH:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A numeric secret (a PIN) is echoed as its text.
        return {str(value)}
    if isinstance(value, dict):
        return set().union(*(_all_strings(item, _depth=_depth + 1) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_strings(item, _depth=_depth + 1) for item in value))
    return set()


def _refusal_detail(response_body: Any, request_raw: Optional[bytes], query: Any = None) -> Any:
    """What a refused write's row keeps of its response: `{"detail": ...}` and nothing else, or `None`.

    A string detail is kept; a validation list keeps each item's `loc` and `msg`; an object keeps its string `code`
    and `message`. A value the request (its JSON body or its query) carried under a secret key is replaced by
    `<redacted>` wherever the detail echoes it, since a key-name redactor cannot see a value quoted inside a sentence."""
    if not isinstance(response_body, dict):
        return None
    detail = response_body.get("detail")
    if isinstance(detail, list):
        detail = [
            {key: item[key] for key in _VALIDATION_ITEM_KEYS if key in item}
            for item in detail if isinstance(item, dict)
        ]
    elif isinstance(detail, dict):
        detail = {key: detail[key] for key in _DETAIL_OBJECT_KEYS if isinstance(detail.get(key), str)}
    elif not isinstance(detail, str):
        return None
    secrets = _secret_strings(query)
    if request_raw:
        try:
            secrets |= _secret_strings(json.loads(request_raw))
        except (ValueError, RecursionError):
            pass
    # Longest first, so a secret containing a shorter one is blanked whole.
    scrubbed = sorted((s for s in secrets if len(s) >= _MIN_SCRUBBED_SECRET), key=len, reverse=True)
    return {"detail": _scrub_strings(detail, scrubbed)} if scrubbed else {"detail": detail}


def _scrub_strings(value: Any, secrets: list[str]) -> Any:
    """`value` with each secret replaced by `<redacted>`, and any string still sharing a `_SECRET_OVERLAP` run with
    one (a truncated or re-quoted echo) blanked whole."""
    if isinstance(value, dict):
        return {key: _scrub_strings(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_strings(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    for secret in secrets:
        if len(secret) >= _SECRET_OVERLAP and any(
            secret[i:i + _SECRET_OVERLAP] in value for i in range(len(secret) - _SECRET_OVERLAP + 1)
        ):
            return REDACTED
    return value


def _redact_path_keys(path: str, body: Any) -> Any:
    keys = RESPONSE_SECRET_KEYS.get(path)
    if not keys or not isinstance(body, dict):
        return body
    return {key: REDACTED if key in keys else value for key, value in body.items()}

# The command bus carries secrets the denylist cannot see (the pasted OAuth `..._submit_code.args.code`).
# The route's own `_credential_safe_args`/`_credential_safe_result` are reused rather than a second denylist; adding `code`/`config` to `SEGMENT_TERMS` would blank
# `status_code`, `exit_code` and every `config` payload.
COMMAND_BUS_PATH = "/api/commands"

# A 404 has no `scope["endpoint"]`; a 405 does (`Match.PARTIAL` updates the scope), so both are
# skipped deliberately: unbounded noise a scanner can open at will.
SKIPPED_WRITE_STATUSES = frozenset({404, 405})

JSON_MEDIA_TYPE = "application/json"

# See `_stored_body`: caller-chosen and outside the body budget.
MEDIA_TYPE_MAX_CHARS = 128

# `String(48)`; a defensive guard, not an expected truncation.
KIND_MAX_CHARS = 48

# `String(255)`, and load-bearing: a failed sign-in records the submitted (unvalidated) email, and an
# over-long one would make the insert raise and silently delete the row recording the attempt.
ACTOR_LABEL_MAX_CHARS = 255

# `String(512)`: a 403'd route with a long `str` path param reaches the insert.
PATH_MAX_CHARS = 512

# Each holds a worker thread and a DB connection from the request pool (`app/db.py`: 20 + 20), so
# kept well under it.
CAPTURE_MAX_INFLIGHT = 16


def _write_excluded(path: str) -> bool:
    return path in WRITE_EXCLUDED_EXACT or any(path.startswith(prefix) for prefix in WRITE_EXCLUDED_PREFIXES)


def _media_type(raw: str) -> str:
    """The bare media type of a `Content-Type` header, lower-cased and without its parameters."""
    return raw.split(";")[0].strip().lower()


def _json_safe(value: Any) -> Any:
    """A path/query param as something `JSONB` can hold (a guard for a future non-`str` converter)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _field_names(body: Any) -> list[str]:
    """The top-level key names of a captured body: the whole of what `/api/credentials` stores."""
    return sorted(str(key) for key in body) if isinstance(body, dict) else []


class _WriteCapture:
    """One mutating request's capture state, and the once-only latch that guarantees one row per request.

    The row is scheduled from the response tee's completion, never from `dispatch`:
    `BaseHTTPMiddleware` consumes `body_iterator` after `dispatch` returns, so a row scheduled there sees
    an empty tee (null `data.response`, anonymous sign-ins).

    Four exits, one latch (`schedule_once`), since a naive `finally` would double every row:
      (a) the tee exhausts normally -> schedule;
      (b) `body_stream()` re-raises the downstream exception -> schedule, then re-raise;
      (c) the client disconnects -> schedule with `client_disconnected: true`;
      (d) an exception before any response -> no tee; `dispatch`'s own wrapper writes the row.
    Rejected: `response.background` fires only on success, so it cannot serve (c) or (d).
    """

    __slots__ = (
        "request", "method", "path", "query", "request_raw", "request_over_ceiling",
        "request_omitted_media", "response_chunks", "response_bytes", "captured_bytes",
        "capture_response", "caller",
        "status", "client_disconnected", "_scheduled",
    )

    def __init__(self, request: Request) -> None:
        self.request = request
        self.method = request.method
        self.path = request.url.path
        self.query = dict(request.query_params)
        self.request_raw: Optional[bytes] = None
        self.request_over_ceiling: Optional[int] = None
        self.request_omitted_media: Optional[str] = None
        self.response_chunks: list[bytes] = []
        self.response_bytes = 0
        self.captured_bytes = 0
        self.capture_response = False
        # Snapshotted now: the row is written after the response, when the request's binding is gone.
        self.caller = request_caller.facts(request)
        self.status: Optional[int] = None
        self.client_disconnected = False
        self._scheduled = False

    # ── request side ──

    async def read_request(self) -> None:
        """Capture the request body; the order of checks is the safety argument.

        The content-type and `Content-Length` checks come strictly before the read: a multipart upload or an
        over-ceiling body is never pulled into memory, and an undeclared length is not read at all.

        Reading here is safe only because `BaseHTTPMiddleware`'s `_CachedRequest` replays a dispatch-read
        `_body` downstream. Never call `request.stream()` or build a fresh `Request(scope, receive)`;
        `test_activity_capture_write.py` guards this. Residual: if `request.body()` raised after a chunk
        arrived, the endpoint would lose it; only `ClientDisconnect` and `MemoryError` get there."""
        media = _media_type(self.request.headers.get("content-type", ""))
        declared = self.request.headers.get("content-length")
        if not media and declared in (None, "0"):
            return  # No body at all — a bare DELETE. `data.request` stays null.
        if media != JSON_MEDIA_TYPE:
            self.request_omitted_media = media or "<unknown>"
            return
        # An undeclared length is treated as unbounded: not read (a chunked upload would buffer whole), and
        # reading under a bound via `request.stream()` would starve the endpoint.
        if declared is None:
            self.request_omitted_media = "<undeclared-length>"
            return
        try:
            size = int(declared)
        except ValueError:
            self.request_omitted_media = "<malformed-length>"
            return
        if size > INPUT_MAX_BYTES:
            self.request_over_ceiling = size
            return
        self.request_raw = await self.request.body()

    # ── response side ──

    def note_response(self, response: Any) -> None:
        self.status = getattr(response, "status_code", None)
        # JSON-only (AC3.1/AC3.3): `redact` walks parsed JSON. A refusal (4xx) is captured too, but only its
        # `detail` is stored (`_refusal_detail`): the reason is what an investigation needs and the status alone
        # does not say. A 5xx body is never stored.
        self.capture_response = (
            isinstance(self.status, int)
            and self.status < 500
            and _media_type(response.headers.get("content-type", "")) == JSON_MEDIA_TYPE
        )

    def _absorb(self, chunk: bytes) -> None:
        """Copy a response chunk aside, up to the read ceiling. The chunk itself is untouched."""
        self.response_bytes += len(chunk)
        if not self.capture_response:
            return
        # A running counter: summing per chunk is quadratic.
        remaining = INPUT_MAX_BYTES - self.captured_bytes
        if remaining > 0:
            kept = chunk[:remaining]
            self.response_chunks.append(kept)
            self.captured_bytes += len(kept)

    async def tee(self, body_iterator: Any):
        """Pass every chunk through untouched while copying the first `INPUT_MAX_BYTES` aside.

        The bytes delivered are the bytes that came in (AC3.3): nothing is added, dropped or reordered.
        `test_activity_capture_write.py` asserts a response byte-for-byte."""
        try:
            async for chunk in body_iterator:
                self._absorb(chunk)
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            # Exit (c): the client left mid-response. The mutation ran, so the row is written, marked because
            # `data.response` is a fragment.
            self.client_disconnected = True
            self.schedule_once()
            raise
        except BaseException:
            # Exit (b). `body_stream()` re-raises the downstream exception after draining the stream.
            self.schedule_once()
            raise
        else:
            self.schedule_once()  # Exit (a).

    # ── the latch ──

    def schedule_once(self, *, status: Optional[int] = None) -> None:
        """Schedule the row, AT MOST ONCE per request, and never raise into the response path (AC1.4)."""
        if self._scheduled:
            return
        self._scheduled = True
        if status is not None:
            self.status = status
        try:
            payload = self._payload()
            if payload is None:
                return
            # AC1.5: everything expensive runs on the worker thread; fire-and-forget. Bounded and dropped (not
            # queued) when saturated: each write holds an executor thread and a pooled connection, and an
            # unbounded queue would 500 real requests. Dropping a best-effort row is the contract; logged loudly.
            if len(_background_tasks) >= CAPTURE_MAX_INFLIGHT:
                log.warning(
                    "write-activity capture DROPPED (%d in flight): %s %s",
                    len(_background_tasks), payload["method"], payload["path"],
                )
                return
            task = asyncio.create_task(asyncio.to_thread(_write_activity_row, payload))
            _background_tasks.add(task)
            task.add_done_callback(_on_capture_done)
        except Exception:
            log.exception("write-activity capture failed")

    def _payload(self) -> Optional[dict[str, Any]]:
        """The cheap, loop-side snapshot handed to the worker, or `None` when there is no row to write.
        Dict and scope reads only: no parse, no redaction, no database."""
        scope = self.request.scope
        endpoint = scope.get("endpoint")
        # No matched route, no row: a 404 has no `kind`.
        if endpoint is None:
            return None
        if self.status in SKIPPED_WRITE_STATUSES:
            return None
        admin_id = getattr(self.request.state, "admin_id", None)
        # No principal, no row (D3): this makes "agents record nothing" true beyond `/api/agent/`. The
        # three public auth routes are the exception (D18).
        if admin_id is None and self.path not in AUTH_WRITE_PATHS:
            return None
        # Deferred: `app.authz` imports this module.
        from app.auth import COOKIE_NAME
        from app.authz.declarations import declaration_of

        route = scope.get("route")
        return {
            "admin_id": admin_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "kind": getattr(endpoint, "__name__", "unknown")[:KIND_MAX_CHARS],
            "route": getattr(route, "path", None),
            "declaration": declaration_of(endpoint),
            "params": {k: _json_safe(v) for k, v in (scope.get("path_params") or {}).items()},
            "query": self.query,
            "request_raw": self.request_raw,
            "request_over_ceiling": self.request_over_ceiling,
            "request_omitted_media": self.request_omitted_media,
            "response_raw": b"".join(self.response_chunks) if self.capture_response else None,
            "response_complete": self.response_bytes <= INPUT_MAX_BYTES,
            # The true size, so a cut-short response records how much was there, not how much was kept.
            "response_bytes": self.response_bytes,
            "client_disconnected": self.client_disconnected,
            # Snapshotted on the loop for thread discipline. Only `/api/auth/logout` carries a cookie (to name
            # its actor); no other row holds a session token, and none is stored.
            "session_cookie": (
                self.request.cookies.get(COOKIE_NAME) if self.path == "/api/auth/logout" else None
            ),
            # Re-resolved off the loop (`_acting_org`): the row is filed in the org an operator's tab acted as.
            "act_as": act_as.requested(self.request.headers),
            "caller": self.caller,
        }


def _stored_body(
    raw: Optional[bytes], *, complete: bool = True, over_ceiling: Optional[int] = None,
    omitted_media: Optional[str] = None, actual_bytes: Optional[int] = None,
) -> Any:
    """One captured body as `ActivityEvent.data` stores it. The rule lives in `serialise_body`; this
    routes the three cases that never reach it.

    A non-dict parsed body is wrapped (`{"body": ...}`): every reader expects a mapping at `data.request`."""
    if over_ceiling is not None:
        # No bytes to serialise, but the size is known; `bytes: 0` would be fabricated.
        return _unparsed(over_ceiling)
    if omitted_media is not None:
        # Capped: this bypasses `serialise_body`, and `Content-Type` is caller-chosen (up to ~16 KB).
        return {"body_omitted": omitted_media[:MEDIA_TYPE_MAX_CHARS]}
    value = serialise_body(raw, complete=complete)
    # `serialise_body` sizes a cut body by the bytes handed in (the ceiling); `actual_bytes` is what
    # the tee counted.
    if actual_bytes is not None and isinstance(value, dict) and value.get("_truncated"):
        value = {**value, "bytes": actual_bytes}
    if value is None or isinstance(value, dict):
        return value
    return {"body": value}


def _subject_noun(feature: str) -> str:
    """A `Declaration.feature` as the same noun a path param would have produced.

    One entity, one spelling in `subject_ref.type`: authz features are plural (`projects`) and
    `SUBJECT_PARAMS` singular (`project`), so a filter on "project" would otherwise miss every create.
    Resolved against `SUBJECT_PARAMS`' own nouns; an unknown feature passes through untouched."""
    nouns = set(SUBJECT_PARAMS.values())
    if feature in nouns:
        return feature
    singular = feature[:-1] if feature.endswith("s") else feature
    return singular if singular in nouns else feature


def _subject_ref(
    params: dict[str, Any], response_body: Any, status: Optional[int], feature: Optional[str],
) -> Optional[dict[str, Any]]:
    """What this row happened to (`{"type": ..., "id": ...}`), or `None` rather than a guess.

    Not from `data.id` (that is the machine-row convention; middleware rows keep ids in `data["params"]`).
    First match wins in `SUBJECT_PARAMS`' declaration order, which is load-bearing (see that constant).
    On a 201 create the id comes from the captured response (why D13 wanted response capture)."""
    for param, noun in SUBJECT_PARAMS.items():
        if param in params:
            return {"type": noun, "id": params[param]}
    if status == 201 and isinstance(response_body, dict) and feature:
        created_id = response_body.get("id")
        if created_id is not None:
            return {"type": _subject_noun(feature), "id": created_id}
    return None


def _write_activity_row(payload: dict[str, Any]) -> None:
    """The worker-thread half of write capture: parse, redact, resolve the actor, INSERT, commit.

    Everything expensive runs here, off the loop (AC1.5), as in `_record_read`; the caller hands over
    raw bytes and cheap scope facts (`_WriteCapture._payload`)."""
    from app.db import SessionLocal

    path = payload["path"]
    status = payload["status"]
    suppress_bodies = _suppress_bodies(path)

    declaration = payload["declaration"]
    request_body = _stored_body(
        payload["request_raw"],
        over_ceiling=payload["request_over_ceiling"],
        omitted_media=payload["request_omitted_media"],
    )
    response_body = _stored_body(
        payload["response_raw"], complete=payload["response_complete"],
        actual_bytes=payload["response_bytes"],
    ) if payload["response_raw"] is not None else None

    # Resolved before the suppression below: D20 blanks credential bodies, not the row's identity, and
    # a created row's id is not a secret.
    subject_ref = _subject_ref(
        payload["params"], response_body, status,
        declaration.feature if declaration is not None else None,
    )

    if path == COMMAND_BUS_PATH:
        # Deferred: `app.routes.commands` imports this module.
        from app.routes.commands import _credential_safe_args, _credential_safe_result

        if isinstance(request_body, dict):
            args = request_body.get("args")
            if isinstance(args, dict):
                request_body = {
                    **request_body,
                    "args": _credential_safe_args(
                        str(request_body.get("command_type") or ""), args
                    ),
                }
        # The reply gets the command bus's audit-row scrub, not the echo's: the 201 body keeps the
        # device-auth `user_code` for the requester (`echo=True`), but `activity_events` is a durable,
        # searchable `system:view` row. The same `_credential_safe_result` the audit row calls, never a second
        # scrubber. The command type is read off `CommandOut`; `""` matches no family (identity scrub).
        if isinstance(response_body, dict) and isinstance(response_body.get("result"), dict):
            response_body = {
                **response_body,
                "result": _credential_safe_result(
                    str(response_body.get("command_type") or ""), response_body["result"]
                ),
            }

    response_body = _redact_path_keys(path, response_body)
    if isinstance(status, int) and status >= 400:
        # From the WHOLE response, not the stored clip: a validation 422 echoing a large `input` passes
        # `BODY_MAX_BYTES`, and the clip's preview would lose the reason. A suppressed path keeps none.
        refusal = None if suppress_bodies else _refusal_detail(
            redact(_whole_response(payload)), payload["request_raw"], payload["query"],
        )
        # Through the one storability rule (NUL, lone surrogate, size): a refusal echoing caller text verbatim
        # must not fail the INSERT and so drop its own row.
        response_body = serialise_body(json.dumps(refusal)) if refusal is not None else None

    if suppress_bodies:
        # D20. Key names only, never a value.
        request_body = {"fields": _field_names(request_body)} if request_body is not None else None
        response_body = {"fields": _field_names(response_body)} if response_body is not None else None

    kind = payload["kind"]
    template = SENTENCE_TEMPLATES.get(kind)
    summary = None
    # A sentence only for a write that happened: templates are past-tense and read the request body, so
    # a refused write would be reported as done. Non-2xx/3xx and status-less rows keep `METHOD /path`; the
    # `label` and status chip still say what was attempted.
    if template is not None and isinstance(status, int) and status < 400:
        try:
            # The redacted bodies, never the raw ones. A template returns `None` when inputs are missing.
            summary = template(request_body, response_body, payload["params"])
        except Exception:
            log.exception("activity sentence template failed (%s)", kind)
    if summary is None:
        # D21: a route that never had prose keeps the raw shape.
        summary = f"{payload['method']} {path}"

    data: dict[str, Any] = {
        "route": payload["route"],
        "access": declaration.access.value if declaration is not None else None,
        "feature": declaration.feature if declaration is not None else None,
        "action": declaration.action if declaration is not None else None,
        # On `/api/credentials` these get the bodies' backstop too (D20: a key-name denylist is fail-open).
        "params": {} if suppress_bodies else redact(payload["params"]),
        "query": {} if suppress_bodies else redact(payload["query"]),
        "subject_ref": subject_ref,
        "request": request_body,
        "response": response_body,
    }
    if payload["client_disconnected"]:
        data["client_disconnected"] = True
    # Under the row's own keys: client-chosen text never replaces one.
    data = {**(payload.get("caller") or {}), **data}

    # Attributed for `_record_read`'s reason, outside the scopes so every read here is this writer's.
    with exclusion_log.caller("middleware:user_activity.write"), SessionLocal() as session:
        # Resolve the actor first, then write under that actor's org. `BaseHTTPMiddleware` runs the app in a
        # child task, so the request's `seam.bind()` never reaches this thread; on the public auth routes the
        # actor is only known once resolved.
        user_id = payload["admin_id"]
        actor_label: Optional[str] = None
        if user_id is None and path in AUTH_WRITE_PATHS:
            # The identity escape, covering exactly this arm: `_auth_actor` reads only `AdminUser` by PK (in
            # `seam.IDENTITY_CLASSES`), and `verify_session_token` reads no table. Who the caller is has not been
            # established, so there is no org to read under.
            with seam.identity_scope():
                user_id, actor_label = _auth_actor(
                    session, path, request_body, _whole_response(payload), status,
                    payload["session_cookie"],
                )
        acting_org = _acting_org(session, user_id, payload.get("act_as"))
        if acting_org is not None:
            data["acting_as"] = act_as.audit_data(*acting_org)
        # Decide the scope before the insert: the arms are different dispositions. An `ExitStack` keeps the
        # single `ActivityEvent(...)` insert in one place.
        with ExitStack() as scope:
            if acting_org is not None:
                # An operator acting as an org: the row is that org's, the actor still the operator.
                scope.enter_context(seam.bound_to(acting_org[0]))
            elif user_id is not None:
                try:
                    # Derived from the actor: a write-audit row belongs to the writer's org.
                    scope.enter_context(seam.bind_from(session, AdminUser, user_id))
                except seam.TenancyRowMissing:
                    # The actor's row is gone (any audited write, not only an auth one: an account deleted
                    # mid-request): still write the row, into org zero as the no-actor case does, rather than
                    # turning it into a lost audit row.
                    scope.enter_context(seam.detached())
                    scope.enter_context(seam.bound_to(ORG_ZERO_ID))
            else:
                # No actor at all (a failed sign-in naming nobody). It belongs to no tenant, and
                # `activity_events.org_id` is NOT NULL, so the operator plane's own org — org zero — holds it:
                # on `operator_scope()` nothing stamped it and the insert refused, losing the row a security
                # reader most needs. `detached()` first so a future bind reaching here cannot make
                # `bound_to()` raise.
                scope.enter_context(seam.detached())
                scope.enter_context(seam.bound_to(ORG_ZERO_ID))
            if payload["admin_id"] is not None:
                # Inside the bind: an unbound read of the actor's row returns nothing once the seam enforces. An
                # acting operator's row is in their own org, not the one bound, so it is read as identity.
                with seam.identity_scope() if acting_org is not None else ExitStack():
                    user = session.get(AdminUser, user_id)
                actor_label = user.email if user is not None else None
            session.add(
                # No `org_id=`: `stamp_org` fills it from the scope entered above, the actor's org or org zero.
                # A column default is foreclosed by
                # `test_tenant_create_surfaces.py::test_no_create_path_falls_back_to_a_well_known_org`.
                ActivityEvent(
                    user_id=user_id,
                    actor_label=actor_label,
                    # Always `"write"`, including on a 4xx/5xx (D14): `status` says it was refused.
                    action_type="write",
                    kind=kind,
                    summary=summary,
                    path=path[:PATH_MAX_CHARS],
                    method=payload["method"],
                    data=data,
                    status=status,
                )
            )
            session.commit()


def _acting_org(session: Session, user_id: Optional[UUID], raw: Optional[str]) -> Optional[tuple[UUID, UUID]]:
    """`(org acted as, operator's own org)` when this write came from an operator's tab acting as an org
    (`app/act_as.py`), else `None`. Decided as the guard decided it; a target the guard refused is no act-as."""
    if user_id is None or raw is None:
        return None
    with seam.identity_scope():
        actor = session.get(AdminUser, user_id)
    if actor is None:
        return None
    try:
        target = act_as.target_org(session, actor, raw)
    except HTTPException:
        return None
    return (target, actor.org_id) if target is not None and actor.org_id is not None else None


def _whole_response(payload: dict[str, Any]) -> Any:
    """The captured response parsed WHOLE, or `None`: for `_auth_actor`, and for a refusal's `_refusal_detail`.

    Not the stored `data.response`: that is clipped at `BODY_MAX_BYTES` into a `preview`, and a sign-in's
    `AdminOut` grows past it with the caller's memberships, which dropped `id`, left the row with no actor
    and no org, and lost it. `_auth_actor` reads only `id` and `email`; a refusal stores only its redacted,
    allowlisted `detail`."""
    raw = payload["response_raw"]
    if raw is None or not payload["response_complete"]:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _auth_actor(
    session: Session, path: str, request_body: Any, response_body: Any, status: Optional[int],
    session_cookie: Optional[str],
) -> tuple[Optional[UUID], Optional[str]]:
    """The actor on the public auth routes (D18), where `request.state.admin_id` is never set.

    Sign-in: read off the whole captured response (`AdminOut`, via `_whole_response`). Sign-out: the
    session cookie answers it. A failed sign-in keeps the submitted email as `actor_label` with a NULL
    `user_id` (whether the account exists is not asserted). The password is already `<redacted>`."""
    if path == "/api/auth/logout":
        # Deferred: `app.auth` imports this module.
        from app.auth import verify_session_token

        admin_id = verify_session_token(session_cookie)
        if admin_id is None:
            return None, None
        user = session.get(AdminUser, admin_id)
        return (admin_id, user.email) if user is not None else (admin_id, None)
    if isinstance(status, int) and status < 400:
        if path in _NESTED_USER_PATHS and isinstance(response_body, dict):
            response_body = response_body.get("user")
        if not isinstance(response_body, dict):
            # A sign-in that succeeded but whose response could not be read whole (past `INPUT_MAX_BYTES`):
            # no actor, and never the submitted email, which would file it as a failed sign-in.
            return None, None
        raw_id = response_body.get("id")
        resolved = optional_uuid(raw_id) if raw_id is not None else None
        email = response_body.get("email")
        return resolved, email if isinstance(email, str) else None
    submitted = request_body.get("email") if isinstance(request_body, dict) else None
    # Through `_text`, the one home for storing unauthenticated input: it caps length (a truncation error
    # would delete this row), replaces `Cc`/`Cf` characters (ANSI escapes, `U+202E`), and refuses lone
    # surrogates.
    return None, _text(submitted)
