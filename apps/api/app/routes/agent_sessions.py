"""The eight node-facing session endpoints: the CP's half of transcript capture. Node-token auth,
daemon-direct (`apps/jaahd/control_plane_api.py`), via `app/routes/agent/auth.py::_check_token`.

  GET  /api/agent/sessions/work                  what this node owes (and reclaims expired leases fleet-wide)
  POST /api/agent/sessions/{sid}/end             a session finished
  POST /api/agent/sessions/{sid}/usage           a RUNNING session's usage so far (the board's live tokens/cost)
  POST /api/agent/sessions/{sid}/events          a batch of an `acp` session's normalised events
  POST /api/agent/sessions/observed              ids the node saw; the CP creates what it lacks
  POST /api/agent/sessions/{sid}/upload-ticket   takes the lease and issues presigned PUTs
  POST /api/agent/sessions/{sid}/transcript      the outcome; the CP writes the manifest and flips
  POST /api/agent/sessions/{sid}/pane-log        a dead run's pane-log tail (`app/pane_logs.py`)

Silent traps, argued at their routes: `running` is never served for upload; an outage leases and burns
nothing; the manifest PUT, not the report, flips `uploaded`. No `known` list (the CP diffs what the
node saw). The CP only presigns, never calls an S3 API (`put_object`)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Text, case, cast, func, select, update
from sqlalchemy.orm import Session

from app import agent_session_lifecycle as lifecycle
from app import attachment_storage
from app import session_transcript_archive as archive
from app import session_transcripts as transcripts
from app import edition_features, slot_state
from app.agent_sessions import (
    END_REASON_RECONSTRUCTED,
    OWED_TRANSCRIPT_STATUSES,
    RUNTIME_ACP,
    STATUS_ENDED,
    STATUS_RUNNING,
    TRANSCRIPT_FAILED,
    TRANSCRIPT_GAVE_UP,
    TRANSCRIPT_NOT_APPLICABLE,
    TRANSCRIPT_UNAVAILABLE,
    TRANSCRIPT_UPLOADED,
    TRANSCRIPT_UPLOADING,
    end_reasons,
)
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.authz.principal import Principal
from app.authz.visibility import visible_node_ref_clause
from app.db import get_session
from app.entity_shape import SYSTEM_ACTOR_ID, StrictModel
from app.events import emit_console_event
from app.model_prices import canonical_model
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.node_clone import resolve_auth_dir
from app.tenancy import seam
from app import node_credentials
from app import session_events
from app import node_log_history, pane_logs
from app.routes.agent.auth import _check_token, caller_node_id
# The per-session cost capture rides this report, since the node's sweep already reads the transcript.
from app.task_metrics_ingest import (
    CapturedUsageMixin,
    MetricsSessionLimit,
    fold_session_metrics,
    mark_live_capture_incomplete,
)
from app.clock import utcnow as _now

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/sessions", tags=["agent"])

# The CP's own manifest PUT: short, since it sits on a node's request.

# Per-phase, because `httpx` applies a scalar to each phase independently (`15.0` ≈ 60s wall clock),
# and these `def` routes hold an anyio worker thread from a shared pool of 40. Sums to 15s.
_MANIFEST_PUT_TIMEOUT = httpx.Timeout(connect=3.0, write=5.0, read=6.0, pool=1.0)

# The states a ticket may be issued for: the owed set only.

# `uploading` is absent: re-ticketing an expired lease would re-stamp `transcript_leased_at` without
# charging an attempt, so `_reclaim_expired_leases` would never match and the row would be renewable
# forever. `/work` never names an `uploading` row anyway.
_TICKETABLE = OWED_TRANSCRIPT_STATUSES


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────

class WorkItem(BaseModel):
    """One row on either work list. `config_dir` is advisory and omitted when it cannot be resolved.

    The omission is the contract: an empty string would be read as a path. The node treats a present
    value as a hint and otherwise scans its own config-dir roster.

    `account_slug` and `meta_dir` must never be added: a slug is not injective across nodes
    (`models.py`, `accounts.slug`), and `meta_dir` is node-side layout derived from `worktree_path` +
    `dispatch_ref`."""

    session_id: str
    config_dir: Optional[str] = None
    worktree_path: Optional[str] = None
    dispatch_ref: Optional[str] = None
    task_id: Optional[int] = None
    # Which agent produced the session (`sessions.agent_kind`) on `awaiting_transcript` rows: the node's sweep
    # reads an `sdk_agent` session's SDK events file instead of a vendor adapter's transcript.
    agent_kind: Optional[str] = None
    # `acp` once the session's event stream has arrived: its usage is the stream's, so the node's sweep reads
    # no vendor parser for it (and the CP refuses one's figures either way).
    runtime: Optional[str] = None


class WorkOut(BaseModel):
    """What this node still owes and whether the CP can currently issue a URL for any of it.

    `storage_available:false` is a normal answer, not an error; `awaiting_transcript` is empty whenever
    it is false, so a node cannot lease work the CP could not pay for."""

    storage_available: bool
    awaiting_transcript: list[WorkItem]
    open: list[WorkItem]
    # Dead runs whose pane-log tail the node should post (`app/pane_logs.py`). Not gated on storage: the tail is
    # stored on the row, not in the bucket.
    awaiting_pane_log: list[WorkItem] = Field(default_factory=list)


class SessionEndIn(StrictModel):
    end_reason: str
    ended_at: Optional[datetime] = None

    @field_validator("ended_at")
    @classmethod
    def _within_the_collectable_window(cls, value: Optional[datetime]) -> Optional[datetime]:
        """`ended_at` reaches the manifest and the operator's page, so it is bounded like the rest."""
        if value is None:
            return None
        if not transcripts.within_the_collectable_window(value, _now()):
            raise ValueError("ended_at is outside the window a real session could have ended in")
        return value


class PaneLogIn(StrictModel):
    """A dead run's pane-log tail, raw, as the node read it. `tail: null` is an answer: the node has no pane log
    for the run, so it is not asked again. Bounded to the node's read window (`pane_logs.PANE_LOG_TAIL_BYTES`)."""

    tail: Optional[str] = Field(default=None, max_length=pane_logs.PANE_LOG_TAIL_BYTES)
    written_at: Optional[datetime] = None


class LiveUsageIn(CapturedUsageMixin):
    """A running session's usage so far, the same shape and bounds as the final report's capture.

    Cumulative for the session, never a delta: the fold overwrites this session's entry, so a lost or
    repeated post costs nothing. `tokens_in`/`tokens_out` are required (a progress post with no counts
    has nothing to say). Their ceiling only keeps the stored JSONB integer, and the board's sum of two,
    inside a signed 64-bit read; the per-class `usage_by_model` counts carry `MAX_TOKEN_COUNT`."""

    model_config = ConfigDict(extra="forbid")

    tokens_in: int = Field(ge=0, le=2**62)
    tokens_out: int = Field(ge=0, le=2**62)


class ObservedItem(StrictModel):
    """One session the node saw, validated here rather than at key derivation.

    An id this shape refuses would be a poison row under a laxer model: listed by `/work` (at the head,
    given a node-chosen `started_at`), 422 on every ticket without moving, relisted forever, starving
    real transcripts."""

    session_id: str = Field(min_length=1, max_length=128)
    config_dir: Optional[str] = Field(default=None, max_length=4096)
    worktree_path: Optional[str] = Field(default=None, max_length=4096)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    @field_validator("session_id")
    @classmethod
    def _keyable(cls, value: str) -> str:
        if not transcripts.is_usable_session_id(value):
            raise ValueError(
                "session_id must be usable as an object-key segment (letters, digits, `.`, `-`, `_`; "
                "at most 128 characters) — an id no key can be derived for cannot become a row"
            )
        return value

    @field_validator("worktree_path", "config_dir")
    @classmethod
    def _a_path_the_cp_would_compose_itself(cls, value: Optional[str]) -> Optional[str]:
        """A path here is relayed to a different node than the one that sent it, so it is checked: without it,
        one node chooses another node's read target via `sessions.cwd`."""
        if value is None:
            return None
        if not transcripts.is_a_plain_absolute_path(value):
            raise ValueError(
                "must be an absolute path with no `..` segment and no control characters — the "
                "control plane relays this to a node that will resolve a file under it"
            )
        return value

    @field_validator("started_at", "ended_at")
    @classmethod
    def _within_the_collectable_window(cls, value: Optional[datetime]) -> Optional[datetime]:
        """A node-reported timestamp has to describe a session the fleet could really have run.

        `started_at` picks the `dt=` partition and is `/work`'s sort key, so an unclamped one heads every
        batch and starves real transcripts out of the node's rolling window."""
        if value is None:
            return None
        if not transcripts.within_the_collectable_window(value, _now()):
            raise ValueError(
                "timestamp is outside the window a collectable transcript could fall in "
                f"(at most {transcripts.OBSERVED_HORIZON.days} days old, and not in the future)"
            )
        return value


class ObservedIn(StrictModel):
    node_id: str = Field(max_length=64)
    # Bounded in the model too, so an unbounded list is not parsed before the route's cap.
    items: list[ObservedItem] = Field(
        default_factory=list, max_length=transcripts.OBSERVED_BATCH
    )


class TicketMemberIn(StrictModel):
    """One member a node is about to upload. `kind` is accepted and ignored: the CP derives it from the name,
    since `kind` picks the key prefix and accepting it would be accepting a key. `extra="forbid"`, so a
    node sending `key` learns it is not honoured."""

    name: str = Field(min_length=1, max_length=transcripts.MEMBER_NAME_MAX)
    kind: Optional[str] = Field(default=None, max_length=32)
    bytes: int = Field(ge=0, le=transcripts.MAX_MEMBER_BYTES)


class TicketIn(StrictModel):
    """A ticket request. A node-supplied key is refused at parse time.

    The `mode="before"` validator runs on the raw input because Pydantic strips unknown fields first;
    `reject_supplied_key` sits beside `extra="forbid"` so the message names the field."""

    # Bounded, so one request cannot ask the CP to presign an arbitrary number of URLs.
    members: list[TicketMemberIn] = Field(default_factory=list, max_length=transcripts.MEMBER_LIMIT)

    @model_validator(mode="before")
    @classmethod
    def _no_node_supplied_key(cls, data: Any) -> Any:
        if isinstance(data, dict):
            transcripts.reject_supplied_key(data)
        return data


class TicketUrl(BaseModel):
    name: str
    url: str


class TicketOut(BaseModel):
    """No keys on the wire: the node PUTs to the URL it is handed and never names a key."""

    bucket: str
    expires_at: datetime
    urls: list[TicketUrl]


class ReportMemberIn(StrictModel):
    """One uploaded member as the node measured it. Every field is bounded: `bytes` is summed into a
    `BigInteger`, and `sha256` is copied into the CP-written manifest."""

    name: str = Field(min_length=1, max_length=transcripts.MEMBER_NAME_MAX)
    bytes: int = Field(ge=0, le=transcripts.MAX_MEMBER_BYTES)
    bytes_raw: int = Field(ge=0, le=transcripts.MAX_MEMBER_BYTES)
    # `^`/`$`, not `\A`/`\Z`: pydantic's Rust `regex` engine rejects those escapes, and its anchors bind to
    # the whole haystack, so this is the strict form.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TranscriptReportIn(CapturedUsageMixin):
    """The upload outcome. Same raw-input key refusal as `TicketIn`: the manifest is built from this payload.

    Inherits `usage_by_model`/`explored_targets` from `app/task_metrics_ingest.py`, which owns their
    ceilings and fold; they ride this report because the sweep already reads the transcript. Both
    default empty, so an older node's report is valid."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(max_length=32)
    error: Optional[str] = Field(default=None, max_length=2000)
    # Bounded to the columns (`Integer`, `BigInteger`), or the write fails after the manifest is stored.
    subagent_count: Optional[int] = Field(default=None, ge=0, le=100_000)
    tokens_in: Optional[int] = Field(default=None, ge=0, le=2**62)
    tokens_out: Optional[int] = Field(default=None, ge=0, le=2**62)
    model: Optional[str] = Field(default=None, max_length=200)
    redaction: Optional[dict[str, Any]] = None
    members: list[ReportMemberIn] = Field(default_factory=list, max_length=transcripts.MEMBER_LIMIT)

    @field_validator("redaction")
    @classmethod
    def _bounded_redaction(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """The one free-form field, copied verbatim into a CP-written object, so it is bounded."""
        if value is not None and len(json.dumps(value, default=str)) > 8192:
            raise ValueError("redaction report is larger than 8192 bytes")
        return value

    @model_validator(mode="before")
    @classmethod
    def _no_node_supplied_key(cls, data: Any) -> Any:
        if isinstance(data, dict):
            transcripts.reject_supplied_key(data)
        return data


# ── Shared helpers ────────────────────────────────────────────────────────────────────────────────


def _config_dir(row: AgentSession, homes: dict[UUID, str], slugs: dict[UUID, str]) -> Optional[str]:
    """The account config dir for a row, or None when no account resolves. Never an empty string.

    Composed through `node_clone.resolve_auth_dir`, never by concatenating or sending a slug. Takes
    prefetched maps: this runs per row on every node's tick, so a per-row SELECT would be an N+1."""
    if row.account_id is None or row.node_id is None:
        return None
    resolved = resolve_auth_dir(homes.get(row.node_id), slugs.get(row.account_id))
    # `resolve_auth_dir` fails closed to `""`, which the node would read as a path; drop it instead.
    return resolved or None


def _account_slugs(session: Session, rows: list[AgentSession]) -> dict[UUID, str]:
    """`accounts.slug` for every account the batch names, in one read."""
    ids = {row.account_id for row in rows if row.account_id is not None}
    if not ids:
        return {}
    return {
        account_id: slug
        for account_id, slug in session.execute(
            select(Account.id, Account.slug).where(Account.id.in_(ids))
        ).all()
    }


def _dispatch_refs(session: Session, rows: list[AgentSession]) -> dict[int, str]:
    """The `_meta/<ref>` stem for every task the batch names, in one read. Spelled `task-<id>` as
    `jaahd/reconcile/enact_launch.py` composes it, so the node does not re-derive the rule."""
    ids = {row.task_id for row in rows if row.task_id is not None}
    if not ids:
        return {}
    return {
        task_id: f"task-{task_id}"
        for task_id in session.scalars(select(Task.id).where(Task.id.in_(ids))).all()
    }


def _work_items(
    session: Session, rows: list[AgentSession], homes: dict[UUID, str], *, with_dispatch: bool
) -> list[WorkItem]:
    """One batch's work items, with every lookup read once up front. Both lists carry `task_id` and
    `agent_kind` (`open` needs them to post live usage, `/usage`: only for a session with a task, and never
    for an SDK-agent session, which has no vendor transcript to meter); `with_dispatch` adds the dispatch
    stem for `awaiting_transcript`, which `open` (the aliveness probe) lacks."""
    slugs = _account_slugs(session, rows)
    refs = _dispatch_refs(session, rows) if with_dispatch else {}
    return [
        WorkItem(
            session_id=row.agent_session_id,
            config_dir=_config_dir(row, homes, slugs),
            worktree_path=row.cwd or None,
            dispatch_ref=refs.get(row.task_id) if with_dispatch else None,
            task_id=row.task_id,
            agent_kind=row.agent_kind,
            runtime=row.runtime,
        )
        for row in rows
    ]


def _row_or_404(session: Session, agent_session_id: str) -> AgentSession:
    row = lifecycle.session_by_agent_id(session, agent_session_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no session recorded for {agent_session_id!r} — report it on "
                "`POST /api/agent/sessions/observed` first, which creates the row"
            ),
        )
    return row


# ── 1. The work list ──────────────────────────────────────────────────────────────────────────────

def _reclaim_expired_leases(session: Session) -> int:
    """Return every expired upload lease, fleet-wide, to `failed`/`gave_up`. Returns how many moved.

    Without this `uploading` is a dead end: not re-served, not re-ticketable, and outside
    `report_transcript`'s unleased arm, so a node that dies holding a ticket strands the row forever.

    Not scoped to the calling node, deliberately: a node-scoped reclaim only frees leases of nodes that
    still call in. Running on any node's `/work` tick reclaims everybody's. Do not add `node_id == node`.

    Limitation: a retired node's row expires once to `failed` (attempts = 1) and stops, since retries
    come only from that node's own work list; the non-terminal message's retry promise is not kept for
    it. The guarantee kept is that no row sits at `uploading` forever.

    One statement, so the read and write cannot disagree with a concurrent report; `failed` vs
    `gave_up` is decided from the incremented counter. Index-covered by `ix_sessions_transcript_sweep`."""
    attempts = AgentSession.transcript_attempts + 1
    exhausted = attempts >= transcripts.MAX_TRANSCRIPT_ATTEMPTS
    deadline = datetime.now(timezone.utc) - transcripts.LEASE_TTL
    reclaimed = session.execute(
        update(AgentSession)
        .where(
            AgentSession.transcript_status == TRANSCRIPT_UPLOADING,
            # `ck_sessions_uploading_holds_a_lease` guarantees the column is non-NULL here.
            AgentSession.transcript_leased_at < deadline,
        )
        .values(
            transcript_status=case((exhausted, TRANSCRIPT_GAVE_UP), else_=TRANSCRIPT_FAILED),
            transcript_attempts=attempts,
            # The reason a human reads inline; the terminal variant must not promise a retry.
            transcript_error=case(
                (
                    exhausted,
                    "the upload lease expired with no report from the node; all "
                    f"{transcripts.MAX_TRANSCRIPT_ATTEMPTS} attempts are used and no further "
                    "attempt will be made",
                ),
                else_=(
                    "the upload lease expired with no report from the node — it will be offered "
                    "again on the next work tick"
                ),
            ),
            # Released, so the row rejoins `awaiting_transcript`, on this tick if it is the caller's.
            transcript_leased_at=None,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    ).rowcount
    if reclaimed:
        # A clock-decided transition on rows the caller may not own, so nothing else announces it. Before the
        # commit: `pg_notify` is transactional.
        session_changed(session)
        log.info("reclaimed %d expired transcript upload lease(s) on a node work tick", reclaimed)
    # Committed here so the work queries read reclaimed rows as committed state and no row lock on
    # another node's session is held for the request.
    session.commit()
    return reclaimed


# `exclude_none` is the contract: `config_dir` must be omitted when unresolved, and `open` items keep
# their six fields (the three probe fields plus `task_id`, omitted for a session with no task,
# `agent_kind` and `runtime`).
@router.get("/work", response_model=WorkOut, response_model_exclude_none=True)
@infra(Tenancy.ORG_SCOPED)
def session_work(
    node_id: str = Query(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> WorkOut:
    """What this node still owes: transcripts to upload, and sessions to probe for aliveness.

    Listing leases nothing and counts no attempt; the lease starts at `/upload-ticket`. The two lists
    are disjoint by construction (`awaiting_transcript` is `ended`, `open` is `running`): a running
    session served for upload would ship a partial transcript. An empty `awaiting_transcript` with
    `storage_available:false` is correct. The one write: fleet-wide lease reclaim first
    (`_reclaim_expired_leases`)."""
    # Derived from the credential, not the query string (`caller_node_id`).
    node = caller_node_id(session, caller, node_id)
    # Before the work queries, so a reclaimed lease is served this tick. Unconditional on storage: a
    # credential outage must not freeze lease expiry.
    _reclaim_expired_leases(session)
    homes = _node_homes(session, [node])
    storage_available = archive.available()

    awaiting: list[WorkItem] = []
    if storage_available:
        rows = session.scalars(
            select(AgentSession)
            .where(
                AgentSession.node_id == node,
                # The disjointness, stated as `ended` so a future status does not silently join.
                AgentSession.status == STATUS_ENDED,
                # The owed set (`app/agent_sessions.py`), covered by the partial sweep index.
                AgentSession.transcript_status.in_(OWED_TRANSCRIPT_STATUSES),
                AgentSession.transcript_attempts < transcripts.MAX_TRANSCRIPT_ATTEMPTS,
            )
            # Oldest first: the node's on-disk window rolls, so the oldest owed row is about to become lost.
            .order_by(AgentSession.started_at.asc(), AgentSession.id.asc())
            .limit(transcripts.WORK_BATCH)
        ).all()
        awaiting = _work_items(session, rows, homes, with_dispatch=True)

    open_rows = session.scalars(
        select(AgentSession)
        .where(AgentSession.node_id == node, AgentSession.status == STATUS_RUNNING)
        .order_by(AgentSession.started_at.asc(), AgentSession.id.asc())
        .limit(transcripts.OPEN_PROBE_BATCH)
    ).all()
    # Unexplained ends only, with a task (the file is named by it), recent, and never answered.
    pane_rows = session.scalars(
        select(AgentSession)
        .where(
            AgentSession.node_id == node,
            AgentSession.status == STATUS_ENDED,
            AgentSession.end_reason.in_(node_log_history.NODE_EXPLAINED_ENDS),
            AgentSession.task_id.is_not(None),
            AgentSession.pane_log_captured_at.is_(None),
            AgentSession.ended_at >= _now() - pane_logs.CAPTURE_WINDOW,
        )
        .order_by(AgentSession.ended_at.desc(), AgentSession.id.desc())
        .limit(pane_logs.WORK_BATCH)
    ).all()
    return WorkOut(
        storage_available=storage_available,
        awaiting_transcript=awaiting,
        open=_work_items(session, list(open_rows), homes, with_dispatch=False),
        awaiting_pane_log=_work_items(session, list(pane_rows), homes, with_dispatch=True),
    )


def _node_homes(session: Session, node_ids: list[UUID]) -> dict[UUID, str]:
    """`nodes.jaah_home` for each id, read once per request."""
    if not node_ids:
        return {}
    return {
        row.id: row.jaah_home or ""
        for row in session.execute(
            select(Node.id, Node.jaah_home).where(Node.id.in_(node_ids))
        ).all()
    }


# ── 2. The end signal ─────────────────────────────────────────────────────────────────────────────

@router.post("/{agent_session_id}/end", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def end_session_route(
    agent_session_id: str,
    body: SessionEndIn,
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Record that a session ended. Idempotent: a repeat for an ended session is a no-op.

    The CP settles most endings itself; this covers a session restarting in place (`_KEEP_HOLD_REASONS`
    keeps the hold) and one the node's probe found dead. `end_reason` is validated against the full
    domain (lifecycle outcomes plus every `runner_breaks` cause)."""
    if body.end_reason not in end_reasons():
        raise HTTPException(
            status_code=422,
            detail=f"end_reason {body.end_reason!r} is not one this control plane records",
        )
    _row_or_404(session, agent_session_id)
    if lifecycle.end_session(
        session,
        agent_session_id=agent_session_id,
        end_reason=body.end_reason,
        ended_at=lifecycle.as_utc(body.ended_at),
    ):
        # Announced here because this route calls `end_session` directly (it must be able to fail the
        # request) rather than via `lifecycle.note_end`, which emits. Without it the page keeps the row as
        # `running` indefinitely. Gated on the return: an idempotent repeat moves nothing and stays silent.
        session_changed(session)
    session.commit()
    return Response(status_code=204)


# ── 2a. A dead run's pane-log tail ──────────────────────────────────────────────────────────────

@router.post("/{agent_session_id}/pane-log", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def pane_log_route(
    agent_session_id: str,
    body: PaneLogIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Store a dead run's pane-log tail, cleaned, redacted and capped (`pane_logs.store_form`). First answer wins:
    a repeat is a no-op, so a later run appending to the same file cannot overwrite the tail of this one."""
    row = _row_or_404(session, agent_session_id)
    if caller.node_id is not None and row.node_id != caller.node_id:
        raise HTTPException(
            status_code=403, detail="a node may only report the pane log of its own sessions"
        )
    # `/work`'s own predicate, minus the window: a pane log is owed only by an ended run with no reason of its
    # own, so an early post cannot pre-empt a running session's real capture.
    if row.status != STATUS_ENDED or row.end_reason not in node_log_history.NODE_EXPLAINED_ENDS:
        raise HTTPException(status_code=409, detail="this session owes no pane log")
    if row.pane_log_captured_at is None:
        if body.tail is not None:
            row.pane_log_tail, row.pane_log_redactions = pane_logs.store_form(body.tail)
            if row.pane_log_tail is None:
                row.pane_log_redactions = None
        row.pane_log_written_at = lifecycle.as_utc(body.written_at)
        row.pane_log_captured_at = _now()
        row.updated_by_id = SYSTEM_ACTOR_ID
        session_changed(session)
    session.commit()
    return Response(status_code=204)


# ── 2b. Live usage while a session runs ─────────────────────────────────────────────────────────

@router.post("/{agent_session_id}/usage", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def report_live_usage(
    agent_session_id: str,
    body: LiveUsageIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Fold a RUNNING session's usage so far into its task's `Task.metrics`, so the board's Tokens and
    Cost columns move within minutes instead of only when the session ends. The node posts every few
    minutes for each `open` work item that names a task.

    A no-op 204, never an error, when the session has ended or has no task: an ended session's numbers
    belong to the final transcript report (`_fold_task_metrics`), and a late live post must never
    overwrite that capture with smaller figures. Also a no-op when the post carries nothing, or the same
    capture as the stored entry (the fold returns False): no row write and no board event.

    LOCK ORDER, task row then session row — `routes/tasks.py::end_attempt`'s order (its task UPDATE, then
    `note_end` on the session). The opposite order deadlocks against an attempt ending at the same moment.
    The session row is then re-read `FOR UPDATE` and re-checked (still `running`, still this task) because
    `/end` and the CP's own settle write it; a post that loses that race reads `ended` and stands down.

    Machinery, not an edit: the fold pins `Task.updated_at` (`fold_session_metrics(live=True)`). Like the
    final fold, a failure is logged and swallowed; the next post retries it."""
    row = _row_or_404(session, agent_session_id)
    # A derived node may only meter its own sessions; the CP's own arms (`node_id is None`) are fleet-wide.
    if caller.node_id is not None and row.node_id != caller.node_id:
        raise HTTPException(
            status_code=403, detail="a node may only report usage for its own sessions"
        )
    task_id = row.task_id
    # One usage source per attempt: an `acp` session is counted from its event stream (`ingest_events`), so a
    # vendor parser's figure for it is never folded.
    if row.runtime == RUNTIME_ACP or task_id is None or not (
        body.usage_by_model or body.tokens_in or body.tokens_out or body.explored_targets
    ):
        session.commit()
        return Response(status_code=204)
    # The task row first (see LOCK ORDER).
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
    locked = session.execute(
        select(AgentSession.status, AgentSession.task_id, AgentSession.runtime)
        .where(AgentSession.id == row.id)
        .with_for_update()
    ).one_or_none()
    # `runtime` re-read too: a first event batch committed since the read above makes the stream the source.
    if (
        locked is None or locked.status != STATUS_RUNNING or locked.task_id != task_id
        or locked.runtime == RUNTIME_ACP
    ):
        session.commit()
        return Response(status_code=204)
    try:
        with session.begin_nested():
            changed = fold_session_metrics(
                session,
                task_id,
                row.agent_session_id,
                tokens_in=body.tokens_in,
                tokens_out=body.tokens_out,
                usage_by_model=body.usage_by_model,
                explored_targets=body.explored_targets,
                usage_incomplete=body.usage_incomplete,
                live=True,
            )
    except Exception:  # noqa: BLE001 — a progress meter may not fail the node's tick
        log.warning(
            "sessions: %s posted live usage for task %s that could not be folded into its metrics",
            row.agent_session_id, task_id, exc_info=True,
        )
        session.commit()
        return Response(status_code=204)
    if changed:
        # So an open board refetches; not a transition, so no activity row (as `rated`). Relayed to Live Cast
        # viewers too (`routes/live_public.py::live_stream`): the public page shows each task's spend.
        emit_console_event(
            session, entity="task", id=task_id, action="metered", record_transition=False
        )
    session.commit()
    return Response(status_code=204)


# ── 2c. The normalised event stream of an `acp` session ─────────────────────────────────────────────

class EventsIn(StrictModel):
    """One batch of §3.1 events (`app/session_events.py`), in any order; `seq` is the node's."""

    #: The task the node ran the session for (`control_plane_api.post_session_events` always sends it); checked
    #: against the row like each event's own `task_id`.
    task_id: Optional[int] = Field(default=None, ge=1)
    events: list[session_events.AgentEvent] = Field(
        min_length=1, max_length=session_events.MAX_BATCH_EVENTS
    )

    @model_validator(mode="after")
    def _bounded(self) -> "EventsIn":
        size = sum(len(json.dumps(event.data, default=str)) for event in self.events)
        if size > session_events.MAX_BATCH_BYTES:
            raise ValueError(f"the batch's data is {size} bytes; the limit is {session_events.MAX_BATCH_BYTES}")
        return self


#: A batch's raw body: the data cap plus room for 500 envelopes.
_EVENTS_MAX_BODY_BYTES = session_events.MAX_BATCH_BYTES + 1024 * 1024


async def _events_body(request: Request) -> EventsIn:
    """The batch, bounded on its RAW bytes before it is parsed — the declared length first, then the bytes actually
    read — as `routes/agent/node_config.py::_node_logs_body`. The 422 names fields, never quotes the input back."""
    oversize = HTTPException(
        status_code=413, detail=f"an event batch must be at most {_EVENTS_MAX_BODY_BYTES} bytes"
    )
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > _EVENTS_MAX_BODY_BYTES:
        raise oversize
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _EVENTS_MAX_BODY_BYTES:
            raise oversize
        chunks.append(chunk)
    try:
        return EventsIn.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_input=False, include_context=False)
        ) from None


class EventsOut(BaseModel):
    #: How many of the batch were new (a re-sent batch answers 0).
    accepted: int
    #: The highest `seq` stored for the session.
    last_seq: int


@router.post("/{agent_session_id}/events", response_model=EventsOut)
@infra(Tenancy.ORG_SCOPED)
def ingest_events(
    agent_session_id: str,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    body: EventsIn = Depends(_events_body),
    session: Session = Depends(get_session),
) -> EventsOut:
    """Store a batch of an `acp` session's events, idempotent on `seq`, and fold its usage.

    The first batch marks the row `runtime=acp`: from then on the stream is the session's ONE usage source,
    and the vendor parser's figures for it are refused (`report_live_usage`, `report_transcript`). Seqs must
    continue the stream without a gap (409 names the next one). A batch adds only its NEW usage items to the
    session's running totals (a re-send adds nothing); a `session_end` item recomputes them from the whole
    stream, so a fold lost to a failed commit is made good at the end — once: the stored `session_end` closes the
    stream (`events_ended`), and a batch adding anything after it is a 409. 503 when this control plane has no
    storage or S3 refuses (the node retries the batch); nothing is marked then.

    LOCK ORDER as `report_live_usage`: the per-session ingest lock (advisory, `session_events.ingest_lock`),
    then the task row (the fold), then this session's row."""
    row = _row_or_404(session, agent_session_id)
    if caller.node_id is not None and row.node_id != caller.node_id:
        raise HTTPException(status_code=403, detail="a node may only post events for its own sessions")
    row_id = row.id
    try:
        return _store_batch(session, row, agent_session_id, body)
    except HTTPException as exc:
        if exc.status_code in _RECORDED_REFUSALS:
            _record_refusal(session, row_id, exc, body)
        raise


# The refusals a node cannot fix by retrying, recorded on the row (`AgentSession.events_refused`): a node retries
# a refused batch forever and logs it only on its own host, so the session would otherwise read "no events" with
# no why. Not 503 (an outage the retry does fix), not 403/404 (no owned row to record on).
# Only refusals raised past the ownership check: a body `_events_body` itself refuses (oversize 413, malformed 422)
# is never recorded — it is refused before the session row is read.
_RECORDED_REFUSALS = frozenset({409, 413, 422})


def _record_refusal(session: Session, row_id: UUID, exc: HTTPException, body: EventsIn) -> None:
    """Persist and COMMIT `events_refused` for the batch about to be refused. Never raises: the refusal is the
    answer the node needs, and a failed bookkeeping write must not turn it into a 500.

    Rolls back first: the refused attempt may hold the ingest lock and a refreshed row, and nothing it did is
    kept (every refusal precedes the batch's writes)."""
    first_seqs = [event.seq for event in body.events]
    refused = {
        "status": exc.status_code,
        "detail": lifecycle.redacted_error(str(exc.detail), 500),
        "at": _now().isoformat(),
        "first_seq": min(first_seqs) if first_seqs else None,
    }
    try:
        session.rollback()
        session.execute(
            update(AgentSession).where(AgentSession.id == row_id).values(events_refused=refused)
        )
        session.commit()
    except Exception:  # noqa: BLE001 — bookkeeping may never replace the refusal the node is owed
        session.rollback()
        log.warning(
            "sessions: could not record the refused event batch (%s) on session row %s; the refusal is still "
            "returned", exc.status_code, row_id, exc_info=True,
        )


def _store_batch(session: Session, row: AgentSession, agent_session_id: str, body: EventsIn) -> EventsOut:
    """`ingest_events` past its ownership check: validate, store and fold one batch. Raises the 4xx refusals
    `ingest_events` records on the row."""
    if body.task_id is not None and row.task_id is not None and body.task_id != row.task_id:
        raise HTTPException(
            status_code=422, detail=f"the batch names task {body.task_id}; the session runs task {row.task_id}"
        )
    for event in body.events:
        if event.session_id != agent_session_id:
            raise HTTPException(
                status_code=422,
                detail=f"event seq {event.seq} names session {event.session_id!r}, not {agent_session_id!r}",
            )
        if event.task_id is not None and row.task_id is not None and event.task_id != row.task_id:
            raise HTTPException(
                status_code=422,
                detail=f"event seq {event.seq} names task {event.task_id}; the session runs task {row.task_id}",
            )
    where = session_events.where_for(row, pin=True)
    if where is None:
        raise HTTPException(
            status_code=503, detail="event storage is not configured on this control plane — retry later"
        )

    session_events.ingest_lock(session, agent_session_id)
    # Re-read under the lock: a concurrent batch may have moved the high-water mark.
    session.refresh(row)
    try:
        last_seq = session_events.check_contiguous(
            (event.seq for event in body.events), last_seq=row.events_last_seq
        )
    except session_events.SeqGap as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if last_seq > session_events.MAX_STREAM_EVENTS:
        raise HTTPException(
            status_code=413, detail=f"a session's stream holds at most {session_events.MAX_STREAM_EVENTS} events"
        )
    if row.events_ended and last_seq > (row.events_last_seq or 0):
        raise HTTPException(status_code=409, detail="the event stream has ended; it accepts no new events")
    closes = not row.events_ended and any(event.kind == "session_end" for event in body.events)
    # New for usage = past the COMMITTED high-water mark, not merely absent from S3: a batch whose PUT landed but
    # whose commit failed is counted when it is re-sent.
    uncounted: dict[int, dict[str, Any]] = {}
    for event in body.events:
        # First wins, as `append` stores it.
        if event.seq > (row.events_last_seq or 0):
            uncounted.setdefault(event.seq, event.stored())
    try:
        added = session_events.append(where, body.events, last_seq=row.events_last_seq)
        stored_total: Optional[Decimal] = None
        if closes:
            totals = session_events.usage_of(session_events.iter_all(where, last_seq=last_seq))
        elif any(event["kind"] == "usage" for event in uncounted.values()):
            start = _stream_totals(session, row)
            stored_total = start.cumulative_cost_usd
            totals = session_events.usage_of((uncounted[seq] for seq in sorted(uncounted)), start)
        else:
            totals = None
    except session_events.EventStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc} — retry the batch") from None
    except transcripts.KeyRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    native_ref = row.native_session_ref or next(
        (event.native_session_ref for event in body.events if event.native_session_ref), None
    )

    measured: dict[str, Any] = {}
    if totals is not None and totals.items:
        measured = {"tokens_in": totals.tokens_in, "tokens_out": totals.tokens_out, "model": totals.model}
    # A batch holding only a running total (`cost_kind: cumulative`) counts no item yet still moves the cost.
    if totals is not None and (totals.items or totals.cumulative_cost_usd not in (None, stored_total)):
        _fold_stream_usage(session, row, totals, live=not (closes or row.status != STATUS_RUNNING))

    became_acp = row.runtime != RUNTIME_ACP
    session.execute(
        update(AgentSession)
        .where(AgentSession.id == row.id)
        .values(
            runtime=RUNTIME_ACP,
            events_last_seq=last_seq,
            events_ended=row.events_ended or closes,
            native_session_ref=native_ref,
            # An accepted batch clears the last refusal: the stream moves again, so it no longer explains anything.
            events_refused=None,
            **measured,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    )
    if became_acp or measured:
        session_changed(session)
    session.commit()
    return EventsOut(accepted=len(added), last_seq=last_seq)


def _stream_totals(session: Session, row: AgentSession) -> session_events.UsageTotals:
    """The session's running stream totals so far: its `Task.metrics` entry when the stream wrote it (an entry the
    vendor parser wrote before the stream arrived is never built on), else the row's own token columns."""
    if row.task_id is None:
        return session_events.UsageTotals(tokens_in=row.tokens_in or 0, tokens_out=row.tokens_out or 0)
    metrics = session.scalar(select(Task.metrics).where(Task.id == row.task_id)) or {}
    entry = (metrics.get("sessions") or {}).get(row.agent_session_id)
    if not isinstance(entry, dict) or entry.get("source") != session_events.STREAM_SOURCE:
        return session_events.UsageTotals()
    return session_events.totals_from_entry(entry)


def _fold_stream_usage(
    session: Session, row: AgentSession, totals: session_events.UsageTotals, *, live: bool
) -> None:
    """Fold an `acp` session's stream usage into its task's `Task.metrics` (the existing usage path), replacing
    the session's entry. The same swallow-and-log savepoint as `_fold_task_metrics`: a cost figure never fails
    the ingest; the next usage batch builds on the last entry that landed, and `session_end` recomputes it whole."""
    if row.task_id is None:
        return
    try:
        with session.begin_nested():
            changed = fold_session_metrics(
                session,
                row.task_id,
                row.agent_session_id,
                tokens_in=totals.tokens_in,
                tokens_out=totals.tokens_out,
                usage_by_model=totals.usage_by_model,
                explored_targets=[],
                live=live,
                reported_cost_usd=_str_or_none(totals.reported_cost_usd),
                reported_cumulative_cost_usd=_str_or_none(totals.cumulative_cost_usd),
                reported_turn_cost_usd=_str_or_none(totals.turn_cost_usd),
                source=session_events.STREAM_SOURCE,
            )
    except (LookupError, MetricsSessionLimit, ValueError):
        log.warning(
            "sessions: %s — stream usage for task %s could not be folded into its metrics",
            row.agent_session_id, row.task_id, exc_info=True,
        )
        return
    if changed:
        emit_console_event(session, entity="task", id=row.task_id, action="metered", record_transition=False)


def _str_or_none(value: Optional[Decimal]) -> Optional[str]:
    return str(value) if value is not None else None


# ── 3. The inverted orphan flow ───────────────────────────────────────────────────────────────────

@router.post("/observed", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def sessions_observed(
    body: ObservedIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """The node's own observed session ids; the CP creates a row for every one it does not hold.

    Inverted flow: the node sends what it saw (capped, horizon-bounded) and the CP diffs, rather than
    the CP shipping an unbounded `known` list every tick.

    A created row is always `ended`/`reconstructed`/`pending` (reported only after the probe says it is
    not alive); `created_by_id` is the system actor. An id the CP already holds is left alone:
    `uq_sessions_agent_session_id` is global, so writing through the conflict would let a node claim a
    row it does not own. The orphan count is read off the rows (`reconstructed_count`), not replied."""
    # No route-level cap: `ObservedIn.items` has `max_length`, validated before this runs.
    # Derived from the credential: this create path's node-supplied input decides which node the orphan
    # rows belong to, so `caller_node_id` refuses a body naming another node.
    node = caller_node_id(session, caller, body.node_id)
    # A session the CP knows is live is never reconstructed: an orphan row is written `ended`, which
    # would end a running session permanently.

    # `ON CONFLICT DO NOTHING` covers ids the CP has a row for; this covers live ones it does not (a fleet
    # runner in `tasks`, a held pool slot's launch session in `worktrees`).
    observed_ids = [item.session_id for item in body.items]
    live = _live_sessions(session, observed_ids)
    # An ACP-hosted run's engine keeps its own transcript under its own id: the row it belongs to is the run's,
    # which records that id as its `native_session_ref` and uploads the run's transcript itself.
    engine_named = _engine_named_sessions(session, node, observed_ids)
    created = 0
    for item in body.items:
        if item.session_id in live:
            log.info(
                "sessions: ignoring observed %s — the control plane holds it as a LIVE session "
                "(an open task's runner, or the holder of a pool slot), so it is not an orphan",
                item.session_id,
            )
            continue
        if item.session_id in engine_named:
            log.info(
                "sessions: ignoring observed %s — it is an ACP-hosted run's engine session, recorded on that "
                "run's own row, so it is not an orphan",
                item.session_id,
            )
            continue
        # One savepoint per item (as `fleet_verdicts.record_intents`): `sessions` has other uniques/FKs, and
        # one failing insert (a node retired mid-request) would otherwise discard the whole batch.
        try:
            with session.begin_nested():
                recorded = lifecycle.record_observed(
                    session,
                    agent_session_id=item.session_id,
                    node_id=node,
                    worktree_ref=None,
                    cwd=item.worktree_path,
                    started_at=lifecycle.as_utc(item.started_at),
                    ended_at=lifecycle.as_utc(item.ended_at),
                )
        except Exception:  # noqa: BLE001 — one refused row may not cost the other 199
            log.warning(
                "sessions: observed %s could not be recorded; the rest of the batch is unaffected",
                item.session_id, exc_info=True,
            )
            continue
        if recorded:
            created += 1
    if created:
        session_changed(session)
    session.commit()
    if created:
        log.info(
            # The resolved uuid, never `body.node_id`: a CRLF surviving `uuid.UUID` could forge a log line.
            "sessions: %d session(s) observed on node %s had no record and were reconstructed",
            created, node,
        )
    return Response(status_code=204)


def _live_sessions(session: Session, ids: list[str]) -> set[str]:
    """Which of `ids` the control plane currently holds as live, by either half of the scope.

    `tasks.runner_session_id` (set at launch, cleared at settle) and `worktrees.launch_session_id` on a
    non-`free` row. Reading only the first would leave pool-slot sessions reconstructable while running.
    Two bounded reads per request."""
    if not ids:
        return set()
    bound = {
        value
        for (value,) in session.execute(
            select(Task.runner_session_id).where(Task.runner_session_id.in_(ids))
        ).all()
        if value
    }
    holding = {
        value
        for (value,) in session.execute(
            select(Worktree.launch_session_id).where(
                Worktree.launch_session_id.in_(ids), Worktree.state != slot_state.FREE
            )
        ).all()
        if value
    }
    return bound | holding


def _engine_named_sessions(session: Session, node: UUID, ids: list[str]) -> set[str]:
    """Which of `ids` one of `node`'s rows already records as its `native_session_ref` — the engine's own id for a
    run the ACP host drove under another (`acp/host.py`). One read per request, over the node's rows
    (`sessions.node_id` is indexed; `native_session_ref` is not)."""
    if not ids:
        return set()
    return {
        value
        for (value,) in session.execute(
            select(AgentSession.native_session_ref).where(
                AgentSession.node_id == node, AgentSession.native_session_ref.in_(ids)
            )
        ).all()
        if value
    }


def session_changed(session: Session) -> None:
    """Queue the live-refresh signal Audit > Sessions listens for, on the caller's transaction.

    One per state-changing write, so the page does not show stale state. Payload, sentinel and org tag
    are single-homed at `app/agent_session_lifecycle.py::_in_a_savepoint`."""
    emit_console_event(
        session, entity="session", id=0, action="changed", org_id=seam.bound_org(),
    )


def reconstructed_count(session: Session, node_id: UUID, since: Optional[datetime] = None) -> int:
    """How many of a node's session rows the CP had no record of until the node reported them.

    A read over the rows rather than a counter, so it cannot drift, scoped by the page's window."""
    statement = select(func.count()).select_from(AgentSession).where(
        AgentSession.node_id == node_id,
        AgentSession.end_reason == END_REASON_RECONSTRUCTED,
    )
    if since is not None:
        statement = statement.where(AgentSession.started_at >= since)
    return int(session.scalar(statement) or 0)


def reconstructed_by_node(
    session: Session,
    since: Optional[datetime] = None,
    *,
    principal: "Principal | None" = None,
) -> list[tuple[Optional[UUID], int]]:
    """The same count as `reconstructed_count`, for the whole fleet at once, grouped by node.

    One round trip instead of one per node. A NULL node_id is kept: an unattributable reconstructed row
    is what the banner exists to admit.

    Node-grant scoped through `principal`, since a count is a disclosure. The default `None` is the
    agent principal (`visible_node_ref_clause` answers `true()`); request callers pass their own."""
    statement = (
        select(AgentSession.node_id, func.count())
        .where(
            AgentSession.end_reason == END_REASON_RECONSTRUCTED,
            visible_node_ref_clause(principal, AgentSession.node_id),
        )
        .group_by(AgentSession.node_id)
    )
    if since is not None:
        statement = statement.where(AgentSession.started_at >= since)
    return [(node_id, int(count)) for node_id, count in session.execute(statement).all()]


# ── 4. The ticket, which is where the lease is taken ──────────────────────────────────────────────

@router.post("/{agent_session_id}/upload-ticket", response_model=TicketOut)
@infra(Tenancy.ORG_SCOPED)
def upload_ticket(
    agent_session_id: str,
    body: TicketIn,
    request: Request,
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> TicketOut:
    """Take the lease and hand back one presigned PUT per member. Counts no attempt.

    The lease starts here, where work starts. Lease and URLs expire together at 15 minutes: a longer
    lease leases nothing useful, a shorter one lets a second ticket race the first's still-valid PUTs.
    Attempts are counted only on node-reported failure and lease expiry.

    503 with no credentials, and the row does not move: an outage costs nothing.

    Keys are derived from the row's `started_at` and the allowlisted member name, so a retry overwrites
    the same objects. A name outside the allowlist is a 422."""
    if not archive.available():
        # Before the row is read: nothing touches the row when the CP cannot pay.
        raise HTTPException(status_code=503, detail="transcript storage unavailable")

    row = _row_or_404(session, agent_session_id)
    # The row's bucket, fixed now if this is its first object: `_complete_upload` writes the manifest
    # there, and the retention sweep and offboarding find an unfinished upload's members there.
    pinned = archive.pin_bucket(session, row)
    where = archive.stored_at(pinned)
    if where is None:
        raise HTTPException(status_code=503, detail="transcript storage unavailable")
    region, bucket, credentials = where
    on_volume = bucket == attachment_storage.LOCAL_BUCKET
    now = _now()
    if row.status != STATUS_ENDED:
        # A running session would upload a partial transcript and then leave the work set forever.
        raise HTTPException(
            status_code=409,
            detail=f"session {agent_session_id} is {row.status}; a transcript is collected once it ends",
        )
    if row.transcript_status not in _TICKETABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session {agent_session_id} is {row.transcript_status}; no upload is owed "
                "(a terminal transcript state is not re-ticketed)"
            ),
        )
    if row.transcript_attempts >= transcripts.MAX_TRANSCRIPT_ATTEMPTS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session {agent_session_id} has used all "
                f"{transcripts.MAX_TRANSCRIPT_ATTEMPTS} upload attempts"
            ),
        )
    try:
        # No `reject_supplied_key` here: it runs on `TicketIn`'s raw input.
        for member in body.members:
            if member.bytes > transcripts.MAX_MEMBER_BYTES:
                raise transcripts.KeyRefused(
                    f"member {member.name!r} is {member.bytes} bytes, above the "
                    f"{transcripts.MAX_MEMBER_BYTES}-byte single-PUT ceiling"
                )
        keyed = transcripts.member_keys(
            session_id=row.agent_session_id,
            started_at=lifecycle.as_utc(row.started_at),
            names=[member.name for member in body.members],
        )
    except transcripts.KeyRefused as exc:
        # 422 and the row does not move: a malformed ticket is the node's bug, not a failed upload.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    sizes = {member.name: member.bytes for member in body.members}
    if not on_volume:
        if not edition_features.enabled("aws"):
            # Unreachable: an edition without `aws` keeps every session on its volume.
            raise HTTPException(status_code=503, detail="transcript storage is not configured here")
        from app.aws import presign
    urls = [
        TicketUrl(
            name=name,
            # The community edition's volume: a URL on this control plane, absolute as the node dialed it (it PUTs
            # exactly the bytes it declared, which the URL signs).
            url=str(request.base_url).rstrip("/") + attachment_storage.presign_upload(
                attachment_storage.local_volume(key), key, content_length=sizes[name]
            )[0],
        )
        if on_volume
        else TicketUrl(
            name=name,
            url=presign.presigned_url(
                method="PUT",
                # Constructed, never accepted: `presigned_url` signs whatever host it is given, and the URL is a
                # bearer token.
                url=presign.s3_object_url(bucket=bucket, key=key, region=region),
                region=region,
                service="s3",
                credentials=credentials,
                amz_date=amz_date,
                expires_seconds=transcripts.LEASE_TTL_SECONDS,
            ),
        )
        for name, key in keyed
    ]

    taken = session.execute(
        update(AgentSession)
        .where(
            AgentSession.id == row.id,
            # Fenced on the state the pre-check observed: two tickets for one session could both pass the check,
            # and the later one would re-stamp the lease for 15 unearned minutes.
            AgentSession.transcript_status.in_(_TICKETABLE),
        )
        .values(
            transcript_status=TRANSCRIPT_UPLOADING,
            # The stamp makes `uploading` reclaimable; `ck_sessions_uploading_holds_a_lease` enforces the pair.
            transcript_leased_at=now,
            # Clear the previous attempt's reason, which the page would render as explaining this attempt.
            transcript_error=None,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    ).rowcount
    if taken:
        session_changed(session)
    session.commit()
    if not taken:
        # The lease went to the first writer. 409 rather than URLs whose lease is not this caller's.
        raise HTTPException(
            status_code=409,
            detail=(
                f"the upload lease for {agent_session_id} was taken by another writer between this "
                f"request's read and its write; it will be offered again once that lease settles"
            ),
        )
    return TicketOut(
        bucket=bucket,
        expires_at=now + transcripts.LEASE_TTL,
        urls=urls,
    )


# ── 5. The report, and the manifest that must land before it counts ───────────────────────────────

# What a node may report. `not_applicable`: the session never wrote a transcript (an ACP run that stored no event),
# terminal like `unavailable`, which is reserved for bytes that existed and were lost.
_REPORT_OUTCOMES = (TRANSCRIPT_UPLOADED, TRANSCRIPT_FAILED, TRANSCRIPT_UNAVAILABLE, TRANSCRIPT_NOT_APPLICABLE)
_UNLEASED_OUTCOMES = (TRANSCRIPT_FAILED, TRANSCRIPT_UNAVAILABLE, TRANSCRIPT_NOT_APPLICABLE)


@router.post("/{agent_session_id}/transcript", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def report_transcript(
    agent_session_id: str,
    body: TranscriptReportIn,
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """What became of the upload. The report alone never flips the row to `uploaded`.

    Ordering: the CP authors `manifest.json`, PUTs it, then flips. A failed PUT leaves `failed` with its
    reason; flip-then-write would show a working download over nothing.

    A CP-side failure costs no attempt (`_record_error`); a node-side one costs one (`_fail`). The reply
    is 204 either way; the node learns by being offered the row again.

    Fenced on a state set: terminal outcomes match nothing, so repeats are no-ops. A repeated `failed` is
    a second pass and costs a second attempt. The unleased arm spans `pending` and `failed`, so after a
    lease expiry charged an upload, the node's late report can charge it again: an accepted cost that
    spends a failing node's budget faster. That arm never settles a live lease.

    A failure reported before any ticket is still a failure: three node paths report without a lease
    (`apps/jaahd/session_sweep.py`), and ignoring them caused head-of-line blocking of the oldest-first
    `WORK_BATCH` forever. So `failed`/`unavailable` on a still-owed row is recorded under `_still_owed`,
    charged as the leased path would. An unleased `uploaded` is never accepted: only a ticket can have
    presigned the URLs."""
    row = _row_or_404(session, agent_session_id)
    if body.status not in _REPORT_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"status {body.status!r} is not an upload outcome ({'|'.join(_REPORT_OUTCOMES)})",
        )
    unleased_terminal = (
        # `status == ended` is the lease's precondition, restated because the lease is not the door here:
        # otherwise a still-running row could be pushed to `unavailable` and leave the owed set permanently.
        row.status == STATUS_ENDED
        and row.transcript_status in OWED_TRANSCRIPT_STATUSES
        and body.status in _UNLEASED_OUTCOMES
    )
    if row.transcript_status != TRANSCRIPT_UPLOADING and not unleased_terminal:
        # Nothing to settle (terminal, duplicate, or an unleased `uploaded`). A no-op, never a second attempt.
        session.commit()
        return Response(status_code=204)
    fence = _still_owed(row) if unleased_terminal else _still_leased(row)

    if row.task_id is not None:
        # LOCK ORDER, as `report_live_usage` and `routes/tasks/runner_settle.py::end_attempt`: the task row before this
        # session's row, which `_record_measurements` updates next and the task fold below would otherwise
        # follow — the reverse order deadlocks against a live post still in flight as the session ends.
        session.execute(select(Task.id).where(Task.id == row.task_id).with_for_update())
    # Re-read under the lock: a first event batch committed since the read above makes the stream the source.
    session.refresh(row, attribute_names=["runtime"])
    # After the guard and on the same fence as the settle, so only an admitted reporter writes
    # measurements (a failed upload still counted real tokens).
    _record_measurements(session, row, body, fence=fence)
    # The cost capture, on the same terms; its own guards are inside. Never for an `acp` session: its usage
    # comes from its event stream alone (`ingest_events`), so the vendor parser's capture is not folded.
    if row.runtime != RUNTIME_ACP:
        folded = _fold_task_metrics(session, row, body)
        if not folded and body.status in (TRANSCRIPT_FAILED, TRANSCRIPT_UNAVAILABLE):
            # No final capture replaced the session's last live entry: flag it as an undercount.
            _mark_live_capture_incomplete(session, row)

    if body.status in (TRANSCRIPT_UNAVAILABLE, TRANSCRIPT_NOT_APPLICABLE):
        _settle(
            session, row, transcript_status=body.status,
            error=lifecycle.redacted_error(
                body.error or (
                    "the node reported the transcript is no longer on disk"
                    if body.status == TRANSCRIPT_UNAVAILABLE else "the session never had a transcript"
                )
            ),
            fence=fence,
        )
        session.commit()
        return Response(status_code=204)

    if body.status == TRANSCRIPT_FAILED:
        _fail(
            session, row,
            lifecycle.redacted_error(body.error or "the node reported an upload failure"),
            fence=fence,
        )
        session.commit()
        return Response(status_code=204)

    _complete_upload(session, row, body)
    session.commit()
    return Response(status_code=204)


def _record_measurements(
    session: Session, row: AgentSession, body: TranscriptReportIn, *, fence: tuple[Any, ...]
) -> None:
    """Record what the pass actually cost, under the caller's own fence.

    `model` is canonicalized to its family (as `app/model_prices.py::merge_usage`), so two spellings of
    one model are one value; unknown ids pass through. Forward-only: earlier rows are not backfilled,
    and nothing groups on this column yet; a future grouping should fold on read. An `acp` session's tokens and
    model are written from its event stream (`ingest_events`), never from this vendor-parsed report."""
    from_stream = row.runtime == RUNTIME_ACP
    measured = {
        name: value
        for name, value in (
            ("subagent_count", body.subagent_count),
            ("tokens_in", None if from_stream else body.tokens_in),
            ("tokens_out", None if from_stream else body.tokens_out),
            ("model", canonical_model(body.model) if body.model is not None and not from_stream else None),
        )
        if value is not None
    }
    if measured:
        session.execute(
            update(AgentSession).where(*fence).values(**measured, updated_by_id=SYSTEM_ACTOR_ID)
        )


def _fold_task_metrics(session: Session, row: AgentSession, body: TranscriptReportIn) -> bool:
    """Fold this report's per-model token capture into the task's `Task.metrics`, if there is one. Returns
    whether a capture was folded (it then replaces any live entry for this session).

    No `task_id` is not an error (console sessions have no task), nor is a report with neither capture
    field. The guard admits either field alone: `usage_by_model` (spend) and `explored_targets` (what the
    pass did) are independent, and the re-exploration benchmark needs targets from cheap sessions too.

    The fold must never cost the transcript report: every failure is logged and swallowed, and the
    savepoint keeps a DB failure from poisoning the settle's transaction."""
    if row.task_id is None or not (
        body.usage_by_model or body.explored_targets or body.usage_incomplete
    ):
        return False
    try:
        with session.begin_nested():
            fold_session_metrics(
                session,
                row.task_id,
                row.agent_session_id,
                # NULL when nothing was measured; the fold stores counts, so contribute zero.
                tokens_in=body.tokens_in or 0,
                tokens_out=body.tokens_out or 0,
                usage_by_model=body.usage_by_model,
                explored_targets=body.explored_targets,
                usage_incomplete=body.usage_incomplete,
            )
    except Exception:  # noqa: BLE001 — a cost figure may not break the transcript lifecycle
        log.warning(
            "sessions: %s reported usage for task %s that could not be folded into its metrics; "
            "the transcript report is unaffected",
            row.agent_session_id, row.task_id, exc_info=True,
        )
        return False
    return True


def _mark_live_capture_incomplete(session: Session, row: AgentSession) -> None:
    """A report that ends the capture's chances with no final capture (`unavailable`, or `failed` with no
    usage) leaves the session's last live entry, if any, as the task's only figure for it: an undercount
    (`task_metrics_ingest.mark_live_capture_incomplete`). Same swallow-and-log savepoint as the fold.

    Not done on the lease-reclaim `gave_up` path (`_reclaim_expired_leases`): that fleet-wide bulk UPDATE
    holds session rows across orgs and would then lock task rows, the reverse of `end_attempt`'s order. Any
    `failed` report on the way there already flagged the entry; a session whose every attempt expired
    unreported keeps an unflagged live entry (accepted gap)."""
    if row.task_id is None:
        return
    try:
        with session.begin_nested():
            mark_live_capture_incomplete(session, row.task_id, row.agent_session_id)
    except Exception:  # noqa: BLE001 — a cost figure may not break the transcript lifecycle
        log.warning(
            "sessions: %s ended without a final capture and its live entry on task %s could not be "
            "flagged incomplete; the transcript report is unaffected",
            row.agent_session_id, row.task_id, exc_info=True,
        )


def _still_leased(row: AgentSession):
    """The predicate every settle carries: this row, and still holding the lease we read.

    `_row_or_404` is a plain SELECT, so a reclaim can move the row before the write; fencing on the
    status makes the loser's UPDATE match nothing."""
    return _still_leased_id(row.id)


def _still_leased_id(row_id: UUID, attempt: Optional[datetime] = None):
    """`_still_leased`, keyed on the id alone, usable after a commit has expired the ORM object.

    `attempt` makes the fence see a re-lease. Status alone misses a row reclaimed and then re-ticketed
    back to `uploading`. After `_complete_upload`'s commit that is reachable (a SIGTERMed replica recovers
    the workflow after a reclaim and re-ticket), and a two-clause fence would land attempt 1's manifest
    on attempt 2's bytes. The stamp is exact (`timestamptz` from one Python instant per ticket).

    `None` drops the clause rather than matching `IS NULL`: in-request callers read and write in one
    transaction where status suffices, and an accidental `None` degrades to that weaker fence."""
    fence = (AgentSession.id == row_id, AgentSession.transcript_status == TRANSCRIPT_UPLOADING)
    if attempt is None:
        return fence
    return (*fence, AgentSession.transcript_leased_at == attempt)


def _still_owed(row: AgentSession):
    """`_still_leased`'s sibling for a report arriving without a lease (the row is still owed); the same
    race argument, one status set over."""
    return (
        AgentSession.id == row.id,
        AgentSession.transcript_status.in_(OWED_TRANSCRIPT_STATUSES),
    )


def _settle(
    session: Session,
    row: AgentSession,
    *,
    transcript_status: str,
    error: str,
    fence: Optional[tuple[Any, ...]] = None,
) -> None:
    """Move a row to a terminal transcript state, releasing the lease. Counts no attempt. `fence` defaults
    to `_still_leased`; a pre-ticket report passes `_still_owed`."""
    if session.execute(
        update(AgentSession).where(*(fence if fence is not None else _still_leased(row))).values(
            transcript_status=transcript_status,
            transcript_error=error,
            transcript_leased_at=None,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    ).rowcount:
        # Gated on the write landing: the fence can legitimately match nothing, and an event for a write that
        # did not happen makes the stream lie.
        session_changed(session)


def _fail(
    session: Session, row: AgentSession, error: str, *, fence: Optional[tuple[Any, ...]] = None
) -> None:
    """One failed attempt: increment, release the lease, and go terminal (`gave_up`) at the ceiling.

    The increment lives here and in lease expiry (`_reclaim_expired_leases`) only; this is the single
    home for that fact. A node that failed before taking a ticket is charged the same way, via
    `_still_owed`.

    The third caller is a CP fault: a failed `uploaded` flip after `manifest.json` landed. It is charged
    because the bytes did land and such faults are often deterministic; uncharged, the row would
    re-upload forever and never reach `gave_up`."""
    # Incremented relationally in one statement, so a concurrent reclaim's increment is not lost.
    attempts = AgentSession.transcript_attempts + 1
    exhausted = attempts >= transcripts.MAX_TRANSCRIPT_ATTEMPTS
    ceiling = transcripts.MAX_TRANSCRIPT_ATTEMPTS
    if session.execute(
        update(AgentSession).where(*(fence if fence is not None else _still_leased(row))).values(
            transcript_status=case((exhausted, TRANSCRIPT_GAVE_UP), else_=TRANSCRIPT_FAILED),
            transcript_attempts=attempts,
            # The attempt number is rendered in SQL from the same expression, so the message still says "3 of 5".
            transcript_error=func.concat(
                error, " (attempt ", cast(attempts, Text), " of ", str(ceiling),
                case((exhausted, "; no further attempt will be made)"), else_=")"),
            ),
            transcript_leased_at=None,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    ).rowcount:
        # Gated on the write landing, as in `_settle`.
        session_changed(session)


def _complete_upload(session: Session, row: AgentSession, body: TranscriptReportIn) -> None:
    """Author the manifest, then hand the PUT and the flip to the durable workflow that pairs them.

    Nothing is ever flipped on a manifest that is not in the bucket; a failure lands `failed` with its
    reason. A node-side defect goes through `_fail` (charged), a CP-side failure through `_record_error`
    (not charged). The exception is `FLIP_FAILED`, charged (see `_fail`).

    Composition stays here, where each exit must charge the right party; the PUT and the flip are
    durable in `app/durable/transcript_upload.py`, whose docstring says why the reclaim is not a
    backstop for them."""
    # A report naming nothing, or no main transcript, is not an upload (it would flip terminal over an
    # empty manifest). Via `_fail`: a node-side defect must consume attempts.
    names = {member.name for member in body.members}
    if transcripts.MAIN_TRANSCRIPT_MEMBER not in names:
        _fail(session, row, (
            f"the node reported `uploaded` without `{transcripts.MAIN_TRANSCRIPT_MEMBER}` "
            f"({len(body.members)} member(s) named); a transcript with no session transcript in it "
            "is not a capture"
        ))
        return

    # The credential is discarded: this checks storage before composing, and `put_manifest` resolves a fresh
    # one, since a workflow argument is checkpointed into DBOS and a credential must never land there.
    # The bucket the ticket fixed on the row (`upload_ticket`); the flip records it again.
    where = archive.stored_at(row.transcript_bucket or archive.org_bucket(session, row.org_id))
    if where is None:
        # Credentials vanished between ticket and report: a CP-side outage, charged nothing.
        _record_error(session, row, "transcript storage became unavailable before the manifest was written")
        return
    region, bucket, _credentials = where
    started_at = lifecycle.as_utc(row.started_at)

    try:
        members = [
            {
                "name": member.name,
                "bytes": member.bytes,
                "bytes_raw": member.bytes_raw,
                "sha256": member.sha256,
            }
            for member in body.members
        ]
        # Validates every name against the allowlist while deriving `kind`.
        manifest_bytes = transcripts.build_manifest(
            session_id=row.agent_session_id,
            agent_kind=row.agent_kind,
            node=lifecycle.node_name(session, row.node_id) or None,
            account=session.scalar(select(Account.slug).where(Account.id == row.account_id))
            if row.account_id else None,
            project=lifecycle.project_name(session, row.project_id) or row.worktree_ref,
            task_id=row.task_id,
            started_at=started_at,
            ended_at=lifecycle.as_utc(row.ended_at),
            end_reason=row.end_reason,
            redaction=body.redaction,
            members=members,
        )
        key = transcripts.manifest_key(session_id=row.agent_session_id, started_at=started_at)
        prefix = transcripts.transcript_prefix(
            session_id=row.agent_session_id, started_at=started_at
        )
    except (transcripts.KeyRefused, KeyError, TypeError, ValueError) as exc:
        _record_error(session, row, f"the manifest could not be composed: {exc}")
        return

    # Commit before the network call: `_reclaim_expired_leases` has no `SKIP LOCKED` and rides a node's
    # `/work` request, so a row pinned across a slow PUT would stall that tick and every lease behind it.

    # Safe because the flip is fenced on the lease. Plain values, so the work below does not depend on ORM
    # state after a commit, and because workflow arguments must be serializable.
    row_id = row.id
    session_id = row.agent_session_id
    # The lease this report settles is the workflow's identity, not `transcript_attempts` (which does not
    # move on a CP failure); see `transcript_upload.upload_workflow_id`.
    attempt = lifecycle.as_utc(row.transcript_leased_at)
    transcript_bytes = sum(member.bytes for member in body.members)
    session.commit()

    # Deferred: the durable module pulls `dbos` and registers a workflow by decorator side effect.
    from app.durable.transcript_upload import (
        CAPTURED,
        FAILED,
        FLIP_FAILED,
        IN_FLIGHT,
        finish_upload,
    )

    # Every write below is fenced on this lease, not merely on `uploading` (`_still_leased_id`).
    fence = _still_leased_id(row_id, attempt)

    # Never raises: the 204 is owed once the commit above landed (`finish_upload`).
    outcome, detail = finish_upload(
        row_id=str(row_id),
        session_id=session_id,
        attempt=attempt,
        region=region,
        bucket=bucket,
        prefix=prefix,
        key=key,
        manifest_bytes=manifest_bytes,
        transcript_bytes=transcript_bytes,
    )
    if outcome == FAILED:
        # Nothing reached the bucket: charged to nobody; offered again next tick. `_record_error` redacts.
        _record_error(session, row, detail or "writing manifest.json failed", fence=fence)
        return
    if outcome == FLIP_FAILED:
        # The manifest is in the bucket and the row could not be flipped: the one charged CP fault (see `_fail`).
        _fail(session, row, lifecycle.redacted_error(
            detail or "the manifest was written but the row could not be marked `uploaded`"
        ), fence=fence)
        return
    if outcome == IN_FLIGHT:
        # A duplicate report under one lease: the workflow already running owns the row, so touch nothing.
        log.info(
            "sessions: %s — a report for this same lease is already being settled; this one is a "
            "duplicate and changes nothing", session_id,
        )
        return
    # The log follows the write and is gated on it. The paired emit is inside the workflow's transaction.
    if outcome == CAPTURED:
        log.info("sessions: %s — manifest.json stored, the transcript is captured", session_id)
        return
    # Not an error: the reclaim handed the row back; the next attempt overwrites the manifest at the same key.
    log.info(
        "sessions: %s — the manifest PUT succeeded but the lease was reclaimed mid-upload; the "
        "row was NOT flipped and the transcript is owed again", session_id,
    )


def _record_error(
    session: Session, row: AgentSession, message: str, *, fence: Optional[tuple[Any, ...]] = None
) -> None:
    """The control plane failed, so the row goes straight back to `failed` and nothing is charged.

    Leaving it `uploading` would let lease expiry charge it 15 minutes later, so five outages would
    `gave_up` a transcript still on the node's disk. A credential outage must burn no attempt; `/work`
    and `/upload-ticket` honour that at their doors, this is the third. `failed` rejoins
    `awaiting_transcript` on the next tick.

    `fence` defaults to `_still_leased`; the caller after `_complete_upload`'s commit passes the
    lease-keyed fence, so a re-leased attempt is not overwritten (`_still_leased_id`)."""
    # Redacted once, used for both: a presigned URL in CloudWatch outlives its 15-minute expiry.
    safe = lifecycle.redacted_error(message)
    if session.execute(
        # Fenced: a reclaim that took this row already settled it.
        update(AgentSession).where(*(fence if fence is not None else _still_leased(row))).values(
            transcript_status=TRANSCRIPT_FAILED,
            transcript_error=safe,
            transcript_leased_at=None,
            updated_by_id=SYSTEM_ACTOR_ID,
        )
    ).rowcount:
        # Gated on the write landing, as in `_settle`.
        session_changed(session)
    log.warning("sessions: %s — %s", row.agent_session_id, safe)


def put_object(
    *, bucket: str, region: str, key: str, body: bytes, credentials: Any,
    timeout: Any = _MANIFEST_PUT_TIMEOUT,
) -> None:
    """PUT one object the CP authored, through a presigned URL. The CP never calls an S3 API.

    Plain `httpx`, no SDK or header signing. Only `host` is signed, so any `Content-Type` or `x-amz-*`
    header is an unexplained 403; the request's own headers are checked with
    `presign.reject_forbidden_headers`, the same predicate the node's uploader uses."""
    if not edition_features.enabled("aws"):
        raise RuntimeError("no S3 in this edition: the volume's writer is `write_volume`")
    from app.aws import presign

    url = presign.presigned_url(
        method="PUT",
        url=presign.s3_object_url(bucket=bucket, key=key, region=region),
        region=region,
        service="s3",
        credentials=credentials,
        amz_date=_now().strftime("%Y%m%dT%H%M%SZ"),
        expires_seconds=transcripts.LEASE_TTL_SECONDS,
    )
    with httpx.Client(timeout=timeout) as client:
        request = client.build_request("PUT", url, content=body)
        presign.reject_forbidden_headers(request.headers.keys())
        response = client.send(request)
    if not 200 <= response.status_code < 300:
        # 2xx or nothing: redirects are off, and a wrong-region `301` stores nothing, so `< 400` as success
        # would flip every session to `uploaded` over an empty bucket.

        # Status only, never the URL (a bearer token); `raise_for_status` would include it.
        raise RuntimeError(f"S3 answered {response.status_code} for the manifest PUT")
