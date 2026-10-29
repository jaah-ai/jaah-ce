"""Operator-facing session routes — the list, the transcript download, a dead run's pane log and the prune (the
Efficiency aggregate is `routes/session_efficiency.py`'s).

`GET /api/sessions` feeds Audit > Sessions (`sessions:view`, node-grant scoped). The prune
(`DELETE /api/sessions/{session_id}`, `POST /api/sessions/stale/delete`) is `sessions:edit` and
node-grant scoped; staleness, its S3 guard and destroyer scope live in `app/sessions_cleanup.py`.
Subject rationale: `app/authz/`'s org-admin tier block.

The download is `sessions:edit` (D4/DA10). Handler and generator are sync `def` (see
`app/session_transcript_archive.py`), and every DB fact is resolved before the `StreamingResponse`
exists (a closed Session silently autobegins). Accepted: no `Content-Length`, no survival across a
deploy, NAT egress per GB.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Numeric, Text, and_, case, cast as sa_cast, delete, exists, func, not_, null, or_, select
from sqlalchemy.orm import Session, aliased
from starlette.background import BackgroundTask

from app import session_transcript_archive as archive
from app import session_transcripts as transcripts
from app import data_access_events, data_residency, hipaa, node_log_history, pane_logs, sessions_cleanup
from app.operator_settings import reads as settings_reads
from app.actors import actor_names as resolve_actor_names
from app.agent_session_lifecycle import engine_label
from app import edition_features, jaah_discovery, plan_terms, runner_terminations, secret_redact, task_lifecycle
from app.agent_sessions import (
    RUNTIME_ACP,
    RUNTIME_LEGACY,
    STATUS_ENDED,
    STATUS_RUNNING,
    STATUSES,
    TRANSCRIPT_FAILED,
    TRANSCRIPT_GAVE_UP,
    TRANSCRIPT_PENDING,
    TRANSCRIPT_STATUSES,
    TRANSCRIPT_UPLOADED,
    TRANSCRIPT_UPLOADING,
    end_reason_label,
    end_reasons,
    inferred_ends,
    shown_ending,
)
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import (
    readable_account_clause, scoped_task_select, visible_node_clause, visible_session_clause,
)
from app.auth import require_user
from app.config import session_transcript_config
from app.db import get_session
from app.events import emit_console_event
from app.entity_shape import get_or_404, optional_uuid
from app.models.agent_sessions import AgentSession
from app.models.audit import ActivityEvent
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Microvm, Node
from app.models.tasks import Task
from app.list_page import like_pattern
from app.node_identity import node_ref
from app.routes.agent_sessions import reconstructed_by_node
from app.routes.audit import _require_aware
from app.session_costs import listed_session_costs

log = logging.getLogger("app.routes.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/{session_id}/transcript.tar.gz", response_class=StreamingResponse, response_model=None)
@feature("sessions", "edit")
def download_session_transcript(
    session_id: UUID,
    request: Request,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Stream this session's transcript as a `.tar.gz` assembled on demand from presigned S3 GETs.

    Members arrive plain under a `session-<id>/` directory beside `manifest.json`. Each member's
    `sha256` is checked in passing; a mismatch or failed fetch aborts the connection with no trailer.
    That is a transport check (the digest is the node's, over bytes the CP never sees).

    429 (with `Retry-After`) when both assembly slots are busy; 409 when the session has no uploaded
    transcript; 503 when the bucket or AWS credentials are unresolvable (normal on a local `./dc` stack).
    """
    plan_terms.require_tenant_switch(session, principal, plan_terms.SESSION_TRANSCRIPTS)
    permit = archive.acquire_permit()
    if permit is None:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{archive.MAX_CONCURRENT_ASSEMBLIES} transcript downloads are already being "
                "assembled; try again shortly"
            ),
            headers={"Retry-After": str(archive.RETRY_AFTER_SECONDS)},
        )
    # Acquire -> return is fenced: an exception here is the one path that could strand a permit.
    try:
        # Scoped as the list is (D13), via `visible_node_ref_clause` in the same WHERE, so an ungranted
        # node's transcript is indistinguishable from a missing session. `principal is None` (agent
        # caller) is never admitted by `require_user`; refusing it keeps the allow-all arm unreachable.
        if principal is None:
            raise HTTPException(status_code=404, detail="no such session")
        row = get_or_404(
            session, AgentSession, session_id, detail="no such session", where=(visible_session_clause(principal),)
        )
        if row.transcript_status != TRANSCRIPT_UPLOADED:
            # The row's own recorded reason, when it has one.
            because = f": {row.transcript_error}" if row.transcript_error else ""
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this session has no transcript to download "
                    f"(transcript_status={row.transcript_status}){because}"
                ),
            )
        config = session_transcript_config()
        if archive.on_volume(row.transcript_bucket):
            # The community edition's volume: read as files, nothing to sign.
            region, bucket, credentials = "", row.transcript_bucket, None
        elif config is None:
            raise HTTPException(
                status_code=503,
                detail="transcript storage is not configured here",
            )
        else:
            region, configured_bucket = config
            # The row's bucket wins (where the bytes went), read in that bucket's own data region: the
            # region has no column, the operator's `data_regions` names it (`data_residency.region_of_bucket`).
            bucket = row.transcript_bucket or configured_bucket
            if bucket != configured_bucket:
                region = data_residency.region_of_bucket(bucket)
            if not edition_features.enabled("aws"):
                # Unreachable: the config above is always None without `aws`.
                raise HTTPException(status_code=503, detail="transcript storage is not configured here")
            from app.aws.container_credentials import resolve_credentials

            credentials = resolve_credentials()
            if credentials is None:
                raise HTTPException(
                    status_code=503,
                    detail="no AWS credentials are available to sign the transcript read",
                )
        # The last DB reads: everything below is derived from plain values (the download record excepted).
        agent_session_id = row.agent_session_id
        started_at = row.started_at
        download_data = {
            "session_id": str(row.id),
            "name": row.name,
            "agent_session_id": agent_session_id,
            "task_id": row.task_id,
            "node_id": str(row.node_id) if row.node_id is not None else None,
        }
        session_org_id = row.org_id
        # Read now, like the actor below: no SQL may run after the record's commit, which hands the connection
        # back before the stream starts.
        phi_on = hipaa.org_is_on(session, session_org_id)
        # Read now: the rollback below expires `_user`, and the record must not reload it.
        downloader_id = _user.id if _user is not None else None
        downloader_email = _user.email if _user is not None else None
        # Release the connection now: `get_session` holds it until return, i.e. across the manifest fetch.
        session.rollback()

        try:
            # Derived, not read off the row, like every member key.
            manifest_key = transcripts.manifest_key(
                session_id=agent_session_id, started_at=started_at
            )
            manifest_bytes, manifest = archive.fetch_manifest(
                bucket=bucket, region=region, key=manifest_key, credentials=credentials
            )
            plan = archive.plan_archive(
                agent_session_id=agent_session_id,
                started_at=started_at,
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                bucket=bucket,
                region=region,
                credentials=credentials,
            )
        except (archive.ManifestUnreadable, transcripts.KeyRefused) as exc:
            log.warning("sessions: transcript manifest for %s — %s", agent_session_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Recorded once the archive is known to be servable, and committed (which hands the connection
        # straight back) before the first byte streams: a failed record is a 500, not an unrecorded download.
        # A HIPAA org's PHI read rides the same commit.
        hipaa.stage_phi_read(
            session, request, record_type="transcript", record_id=agent_session_id,
            task_id=download_data["task_id"], org_id=session_org_id, on=phi_on,
            user_id=downloader_id, actor_label=downloader_email,
        )
        data_access_events.record_committed(
            session,
            data_access_events.TRANSCRIPT_DOWNLOADED,
            f"Downloaded the transcript of session {agent_session_id}",
            data=download_data,
            org_id=session_org_id,
            user_id=downloader_id,
            actor_label=downloader_email,
        )

        response = StreamingResponse(
            archive.stream_archive(plan, permit),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{plan.filename}"',
                "Cache-Control": "no-store",
                # nginx would otherwise buffer the whole archive before forwarding a byte.
                "X-Accel-Buffering": "no",
            },
            # The other release path: a client disconnecting before the generator starts. `Permit` is
            # single-shot, so both firing is a no-op.
            background=BackgroundTask(permit.release),
        )
    except BaseException:
        permit.release()
        raise
    return response


class PaneLogOut(BaseModel):
    """A dead run's pane-log tail, as stored (`app/pane_logs.py`: cleaned, redacted, capped), and what places it."""

    id: UUID
    name: Optional[str] = None
    node: Optional[str] = None
    node_id: Optional[str] = None
    task_id: Optional[int] = None
    task_title: Optional[str] = None
    end_reason: Optional[str] = None
    # `end_reason` in words (`agent_sessions.end_reason_label`), so the page carries no copy of the vocabulary.
    end_reason_label: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    captured_at: datetime
    # The file's mtime on the node: later than `ended_at` when a later run of the task appended to it.
    written_at: Optional[datetime] = None
    # `written_at` is well after `ended_at`, so the tail may show a later run of the same task.
    written_after_end: bool = False
    redactions: int = 0
    redaction_confidence: str
    text: str


@router.get("/{session_id}/pane-log", response_model=PaneLogOut)
@feature("sessions", "edit")
def get_session_pane_log(
    session_id: UUID,
    request: Request,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> PaneLogOut:
    """This session's pane-log tail. `sessions:edit`, the transcript download's key and for its reason: the pane
    is what the agent showed, the same content a transcript holds. Scoped as the list is; 404 for a session not
    visible or with no tail. A HIPAA org's read is recorded like a transcript's."""
    plan_terms.require_tenant_switch(session, principal, plan_terms.SESSION_TRANSCRIPTS)
    if principal is None:
        raise HTTPException(status_code=404, detail="no such session")
    found = session.execute(
        select(AgentSession, Node.name)
        .join(Node, Node.id == AgentSession.node_id, isouter=True)
        .where(AgentSession.id == session_id, visible_session_clause(principal))
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail="no such session")
    row, node_name = found
    if row.pane_log_tail is None:
        raise HTTPException(status_code=404, detail="this session has no pane log")
    # Through `scoped_task_select`, as the list's titles: a task the reader cannot reach gets its id only.
    task = None if row.task_id is None else session.scalars(
        scoped_task_select(principal).where(Task.id == row.task_id)
    ).first()
    end_reason, _ = shown_ending(row.end_reason, row.end_detail)
    out = PaneLogOut(
        id=row.id, name=row.name, node=node_name, node_id=str(row.node_id) if row.node_id else None,
        task_id=row.task_id, task_title=task.title if task else None, end_reason=end_reason,
        end_reason_label=end_reason_label(end_reason), started_at=row.started_at,
        ended_at=row.ended_at, captured_at=row.pane_log_captured_at, written_at=row.pane_log_written_at,
        written_after_end=pane_logs.written_after_end(row.pane_log_written_at, row.ended_at),
        redactions=row.pane_log_redactions or 0, redaction_confidence=transcripts.REDACTION_CONFIDENCE,
        # Tidied again as served, so a tail stored before `pane_logs.tidy` reads the same as a new one.
        text=pane_logs.tidy(row.pane_log_tail),
    )
    hipaa.record_phi_read(
        session, request, record_type="pane_log", record_id=row.agent_session_id, task_id=row.task_id,
        org_id=row.org_id, user=_user,
    )
    return out


# ── The operator's list ───────────────────────────────────────────────────────────────────────────
#
# Shaped after `routes/credentials/reads.py::list_credentials`: 1-based offset pagination, an
# `{items, total, page, per}` envelope, and one filter list for both count and page. Sort is server-side
# (it must compose with pagination), and every order ends in `AgentSession.id` so it is total.

PER_PAGE = 25
_PER_MAX = 200
_PAGE_MAX = 1_000_000
_Q_MAX_LEN = 200

# The default is a window (the operator's first question is "what ran last night"); `all` is kept for
# forensics.
WINDOWS: dict[str, Optional[timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
DEFAULT_WINDOW = "24h"
# A window holding at most this many rows is "tiny": the list then counts each wider window (`wider_windows`),
# so one stray row does not read as the whole history.
_WIDER_HINT_AT_MOST = 3

# A `pending`/`uploading` row older than this is late and rendered in the warning tone. Only "late by
# the lease clock", not "stuck": expired `uploading` leases are reclaimed by
# `routes/agent_sessions.py::_reclaim_expired_leases` on whichever node next polls, and the prune
# clears neither state (see `_why_protected`). Served as `overdue_after_seconds` so the page has no copy.
STALE_TRANSCRIPT_AFTER = transcripts.LEASE_TTL

# Operands are cast before summing: each half admits `le=2**62` on the wire, and a `bigint` sum
# overflows (an error, not a wrap), which would 500 `sort=tokens`. Casting the sum does nothing.
# The CASE keeps "no count" NULL (sorted last by `nullslast()`) instead of a coalesced zero, while one
# stated half still sums. The sum stays in the `when` branch so the type comes from the cast operands.
_TOKENS = case(
    (
        or_(AgentSession.tokens_in.isnot(None), AgentSession.tokens_out.isnot(None)),
        sa_cast(func.coalesce(AgentSession.tokens_in, 0), Numeric(40, 0))
        + sa_cast(func.coalesce(AgentSession.tokens_out, 0), Numeric(40, 0)),
    ),
    else_=null(),
)

# The closed set of orderings; anything else is a 422, never a silent default.
SORTS: dict[str, Any] = {
    "name": AgentSession.name,
    "node": Node.name,
    "task": AgentSession.task_id,
    "status": AgentSession.status,
    "started_at": AgentSession.started_at,
    "ended_at": AgentSession.ended_at,
    "tokens": _TOKENS,
    "transcript_status": AgentSession.transcript_status,
}


class EndReasonOption(BaseModel):
    """One End reason filter value: the shown `end_reason` (`agent_sessions.shown_ending`) and its words."""

    value: str
    label: str
    # A break cause no relaunch clears (`runner_causes.TERMINAL_CAUSES`): its cause line explains the row's
    # other alarms, so the page folds them.
    terminal: bool = False
    # Sessions showing this reason under every OTHER filter (window, status, node, …), so the page offers the
    # reasons present rather than the whole vocabulary.
    count: int = 0


def _end_reason_options(session: Session, filters: list[Any]) -> list[EndReasonOption]:
    """The whole `end_reason` domain, in words and counted, so the page's filter carries no copy of the
    vocabulary. One query: a `COUNT(*) FILTER` per reason over `_shown_end_reason_clause`, the filter's own
    predicate, so a count is exactly the rows choosing that reason would list."""
    from app.runner_causes import TERMINAL_CAUSES

    reasons = end_reasons()
    counts = session.execute(
        select(*(func.count().filter(_shown_end_reason_clause(reason)) for reason in reasons))
        .select_from(AgentSession)
        .join(Node, Node.id == AgentSession.node_id, isouter=True)
        .where(*filters)
    ).one()
    return [
        EndReasonOption(
            value=reason, label=end_reason_label(reason) or reason, terminal=reason in TERMINAL_CAUSES, count=count,
        )
        for reason, count in zip(reasons, counts, strict=True)
    ]


class NodeOption(BaseModel):
    """One Node filter value: a node holding sessions under every OTHER filter, valued by uuid (`?node=`)."""

    value: str
    # The node's name; its uuid when the node row is gone.
    label: str
    count: int = 0


def _node_options(session: Session, filters: list[Any]) -> list[NodeOption]:
    """The nodes the Node filter can offer: those holding a session under `filters` (the request's own,
    minus `node`), so the choice is not limited to the rows on screen. One grouped query, by name."""
    rows = session.execute(
        select(AgentSession.node_id, Node.name, func.count())
        .select_from(AgentSession)
        .join(Node, Node.id == AgentSession.node_id, isouter=True)
        .where(AgentSession.node_id.is_not(None), *filters)
        .group_by(AgentSession.node_id, Node.name)
    ).all()
    options = [NodeOption(value=str(node_id), label=name or str(node_id), count=count) for node_id, name, count in rows]
    return sorted(options, key=lambda option: option.label.casefold())


class OrphanCount(BaseModel):
    """One node's count of sessions it observed that the control plane had no record of."""

    node: Optional[str] = None
    # The node's uuid; `node` is its display name.
    node_id: Optional[str] = None
    count: int


class AgentRefOut(BaseModel):
    """Which SDK agent ran an `sdk_agent` session (`sessions.agent_ref`): shown as name, version and short
    commit, never a raw id or slug (AC-4)."""

    agent_id: Optional[str] = None
    display_name: str = ""
    version: str = ""
    commit: Optional[str] = None


class SessionOut(BaseModel):
    id: UUID
    # The readable identifier (`agent_session_lifecycle.py::display_name`); the page ellipsises with
    # CSS rather than truncating.
    name: str
    agent_session_id: str
    agent_kind: str
    # Set on `sdk_agent` rows only; null for every built-in session.
    agent_ref: Optional[AgentRefOut] = None
    # The built-in engine's label (`agent_session_lifecycle.engine_label`); null on `sdk_agent` rows.
    engine: Optional[str] = None
    # `acp` when the session has a normalised event stream (`app/session_events.py`), else `legacy`.
    runtime: str = RUNTIME_LEGACY
    # The runtime the dispatch chose (`models.AgentSession.dispatched_runtime`), display only; null when not known.
    dispatched_runtime: Optional[str] = None
    # The node's display name; `node_id` its uuid, what `?node=` filters on.
    node: Optional[str] = None
    node_id: Optional[str] = None
    # The credential account the session ran on, by name; null when none was recorded or the caller may not read it
    # (`readable_account_clause`).
    account: Optional[str] = None
    task_id: Optional[int] = None
    task_title: Optional[str] = None
    worktree_ref: Optional[str] = None
    branch: Optional[str] = None
    holder: Optional[str] = None
    status: str
    end_reason: Optional[str] = None
    # `end_reason` in words (`agent_sessions.end_reason_label`).
    end_reason_label: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    # Dollars, a decimal string, by Efficiency's per-session rule (`session_costs.listed_session_costs`);
    # null when unpriced or unknown, never `0`.
    cost_usd: Optional[str] = None
    subagent_count: Optional[int] = None
    transcript_status: str
    # Why, not just that: every non-`uploaded` state owes the operator a reason.
    transcript_error: Optional[str] = None
    transcript_bytes: Optional[int] = None
    transcript_attempts: int = 0
    transcript_leased_at: Optional[datetime] = None
    # Whether the CP still holds this session live (`stale_clause`'s liveness conjunct), not derivable
    # on the page from `task_id`/`worktree_ref`. Defaults False, the safe rolling-deploy direction (a
    # refused delete, never a hidden allowed one).
    held_live: bool = False
    # Why a session ended when nothing reported it: its node's warning-or-worse journal lines just before the end,
    # each `"HH:MM:SSZ <unit>: <message>"`, newest first (`node_log_history.end_lines`, which owns the window,
    # the redaction and the same-org rule). Set only on an `end_reason` in `NODE_EXPLAINED_ENDS`; `[]` otherwise.
    node_errors: list[str] = Field(default_factory=list)
    # A dead run's pane-log tail (`pane_logs.state`): `captured` (readable at `/{id}/pane-log`), `missing` (the node
    # had none), null (never asked).
    pane_log_state: Optional[str] = None
    # The cause sentence behind `end_reason` (`models.AgentSession.end_detail`); null when nothing said why.
    end_detail: Optional[str] = None
    # The highest event `seq` stored for an `acp` session; null when none was.
    events_last_seq: Optional[int] = None
    # The last event batch the CP refused (`{status, detail, at, first_seq}`); null when none was.
    events_refused: Optional[dict[str, Any]] = None
    # An ended `acp` session (by its stream or its dispatch) that stored no event: the page says so rather than
    # rendering an empty stream as a quiet one. Derived here so both consoles read one rule.
    no_events: bool = False
    # The node's sentence for this run's MicroVM when it `failed` (`_vm_failures`): a VM that never ran holds no
    # transcript, so the page says why instead of waiting for an upload. Null otherwise.
    vm_failure: Optional[str] = None
    # A `running` row whose task attempt is over though nothing reported its end (`_attempts_over`): the sentence
    # saying why, so the page shows it ended rather than "still running". Null for every other row.
    attempt_over: Optional[str] = None
    # The node's `{online|offline|unknown}` (`jaah_discovery.status_and_last_seen`), set only on a row still owing a
    # transcript: an offline node's upload waits for it, which "overdue" would misstate. Null otherwise.
    node_status: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None


class UnsessionedAttemptOut(BaseModel):
    """A task attempt that ended with no session row: its `task_note:runner_terminated` note, with no session of
    that task started since the task's previous termination (`_unsessioned_attempts`). A launch that died before
    recording a session is otherwise invisible on this page."""

    task_id: int
    # Null when the viewer cannot read the task (`scoped_task_select`) — never the case for a served item.
    task_title: Optional[str] = None
    at: datetime
    cause: Optional[str] = None
    # `runner_terminations.label_for(cause)`, so the page carries no copy of the vocabulary.
    label: str
    detail: Optional[str] = None
    # The node's display name, when the note names a node this viewer reaches.
    node: Optional[str] = None


class WindowCount(BaseModel):
    window: str
    total: int


class SessionListOut(BaseModel):
    items: list[SessionOut]
    # The count of what the filters select, not of the table (AC-5.2).
    total: int
    page: int
    per: int
    # No filters, no window: lets the empty state tell "clear your filters" from "nothing recorded yet".
    total_unfiltered: int
    window: str
    # Whether this CP can serve a transcript at all (false on local stacks); the page disables downloads.
    storage_available: bool
    storage_reason: Optional[str] = None
    # Pinned `best_effort` (`session_transcripts.REDACTION_CONFIDENCE`), shown as a caveat on downloads.
    redaction_confidence: str
    # How old a `pending`/`uploading` row must be to be late (`STALE_TRANSCRIPT_AFTER`); served so the
    # page has no copy.
    overdue_after_seconds: int
    # How old an untranscribed row must be before the page offers deletion: an operator knob
    # (`sessions_cleanup.STALE_SESSION_AFTER_HOURS_KEY`), so it must come from the server.
    stale_after_seconds: int
    # The bulk button's per-press ceiling (`sessions_cleanup.MAX_DELETE_PER_CALL`), needed before any
    # delete; the same name as on the delete response.
    max_per_call: int
    # The stale count, node-grant scoped with the identical principal `POST /stale/delete` gets, so the
    # banner names exactly the set the button removes. Not narrowed by filters or window.
    stale_count: int
    # Sessions a node reported that the CP never recorded, over the same window, so "we track every
    # session" stays falsifiable.
    orphans: list[OrphanCount]
    # The End reason filter's vocabulary, in `end_reasons()` order, each counted under the other filters.
    end_reasons: list[EndReasonOption] = Field(default_factory=list)
    # The Node filter's choices, counted under the other filters (`_node_options`).
    node_options: list[NodeOption] = Field(default_factory=list)
    # Each window wider than this one, counted under the same filters, widest last — served only when this window
    # holds at most `_WIDER_HINT_AT_MOST` rows (else empty, as for `all` and a custom range).
    wider_windows: list[WindowCount] = Field(default_factory=list)
    # Attempts that ended with no session row, newest first, capped at `_UNSESSIONED_MAX`. Page 1 of an unfiltered
    # (or task-filtered) list only: they match no session filter, so under one they would be noise.
    unsessioned_attempts: list[UnsessionedAttemptOut] = Field(default_factory=list)


# How long a `None` storage answer is reused. `resolve_credentials()` never caches `None`, which is
# right per session-end but not for a list every tab refetches: each request would hold a sync
# threadpool thread for up to ~3s. Short enough to pick up a role promptly.
_STORAGE_PROBE_TTL = timedelta(seconds=30)
# One tuple, read once under a lock (as `operator_settings/`): the lock guards a concurrent reset, and
# binding to a local keeps check and read on the same object.
_storage_probe_lock = threading.Lock()
_storage_probe: tuple[datetime, tuple[bool, Optional[str]]] | None = None


def _storage_state() -> tuple[bool, Optional[str]]:
    """Can a transcript be served here, and if not, why not — in words both consoles can read.

    Unconfigured bucket (normal locally) and absent credentials get different sentences; neither is an
    error. Memoized for `_STORAGE_PROBE_TTL`."""
    global _storage_probe
    now = datetime.now(timezone.utc)
    with _storage_probe_lock:
        cached = _storage_probe
    if cached is not None and now - cached[0] < _STORAGE_PROBE_TTL:
        return cached[1]
    # Probed outside the lock so a slow probe does not serialize requests; concurrent cold probes store
    # the same answer.
    answer = _resolve_storage_state()
    with _storage_probe_lock:
        _storage_probe = (now, answer)
    return answer


def reset_storage_probe() -> None:
    """Forget the memoized answer, so the next `_storage_state()` probes for real.

    Public so tests and config changes can invalidate rather than wait out the TTL."""
    global _storage_probe
    with _storage_probe_lock:
        _storage_probe = None


def _resolve_storage_state() -> tuple[bool, Optional[str]]:
    """The uncached probe `_storage_state` memoizes."""
    # Scope-neutral wording: a tenant admin reads this verbatim (`app/tenancy/copy.py`).
    if archive.on_volume(None):
        return True, None
    unconfigured = (False, (
        "Transcript storage is not configured here, so transcripts cannot be downloaded. "
        "Sessions are still recorded."
    ))
    if session_transcript_config() is None:
        return unconfigured
    if not edition_features.enabled("aws"):
        # Unreachable: the config above is always None without `aws`.
        return unconfigured
    from app.aws.container_credentials import resolve_credentials

    if resolve_credentials() is None:
        return False, (
            "No AWS credentials are available to sign a transcript read here, so downloads are "
            "unavailable. Sessions are still recorded."
        )
    return True, None


def _list_filters(
    *,
    window: str,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    status: Optional[str],
    transcript_status: Optional[str],
    stale: bool,
    end_reason: Optional[str] = None,
    node: Optional[str],
    task: Optional[int],
    q: Optional[str],
    principal: Principal | None,
    now: datetime,
    pane_log: bool = False,
    dead: bool = False,
) -> list[Any]:
    """The one filter list, handed to both the count and the page query, so `total` always describes
    the rows."""
    filters: list[Any] = []

    horizon = WINDOWS[window]
    if horizon is not None:
        filters.append(_window_clause(horizon, now))

    # The custom range: inclusive bounds on `started_at`, composed with the window (the page sends
    # `window=all` beside it). No running-row exemption: an explicit range asks when sessions started.
    if since is not None:
        filters.append(AgentSession.started_at >= since)
    if until is not None:
        filters.append(AgentSession.started_at <= until)

    if status is not None:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"unknown status '{status}'")
        # The page's own rule: a `running` row whose attempt is over (`_attempts_over`) reads ended, so the
        # Running filter leaves it out and the Ended filter takes it.
        over = _attempt_over_clause(principal, now)
        if status == STATUS_RUNNING:
            filters.append(and_(AgentSession.status == STATUS_RUNNING, not_(over)))
        elif status == STATUS_ENDED:
            filters.append(or_(AgentSession.status == STATUS_ENDED, and_(AgentSession.status == STATUS_RUNNING, over)))
        else:
            filters.append(AgentSession.status == status)

    if transcript_status is not None:
        if transcript_status not in TRANSCRIPT_STATUSES:
            raise HTTPException(
                status_code=422, detail=f"unknown transcript status '{transcript_status}'"
            )
        filters.append(AgentSession.transcript_status == transcript_status)

    if end_reason is not None:
        if end_reason not in end_reasons():
            raise HTTPException(status_code=422, detail=f"unknown end reason '{end_reason}'")
        filters.append(_shown_end_reason_clause(end_reason))

    if stale:
        # The delete's own predicate, imported, so the preview is exactly what the button takes
        # (`app/sessions_cleanup.py`). Its own parameter, not a `transcript_status` value (it spans two
        # columns). Composes with the window; the page asks for `window=all` alongside.
        filters.append(sessions_cleanup.stale_clause(sessions_cleanup.stale_cutoff(now)))
        # Scoped with the delete's clause (no NULL arm), not the read clause: this is a destruction
        # preview, and NULL-node rows would otherwise be listed yet uncountable and undeletable. The
        # unfiltered list still shows them.
        filters.append(sessions_cleanup.deletable_node_ref_clause(principal, AgentSession.node_id))

    if node:
        # The node uuid: names are per org, so a name filters nothing (`node_ref` resolves it to NULL).
        filters.append(AgentSession.node_id == node_ref(node))

    if task is not None:
        filters.append(AgentSession.task_id == task)

    if pane_log:
        filters.append(AgentSession.pane_log_tail.is_not(None))

    if dead:
        filters.append(dead_run_clause())

    if q:
        # LIKE metacharacters are literal, so a pasted path matches itself.
        pattern = like_pattern(q)
        # The session's own columns only, not `Task.title`: a LIKE over an unreadable column is an
        # oracle. Search by task via the exact `task` filter; `name` embeds the task id.
        # A row's own id matches exactly: it is what a pane-log address and a copied row id carry, so
        # `/sessions?q=<that id>` lands on its row too.
        row_id = optional_uuid(q.strip())
        filters.append(
            or_(
                *([AgentSession.id == row_id] if row_id is not None else []),
                AgentSession.name.ilike(pattern, escape="\\"),
                AgentSession.agent_session_id.ilike(pattern, escape="\\"),
                AgentSession.worktree_ref.ilike(pattern, escape="\\"),
                AgentSession.branch.ilike(pattern, escape="\\"),
                AgentSession.holder.ilike(pattern, escape="\\"),
            )
        )

    return filters


def _window_clause(horizon: timedelta, now: datetime) -> Any:
    """Started within `horizon` — or running: a running session is never windowed out, it is the most
    interesting row."""
    return or_(AgentSession.started_at >= now - horizon, AgentSession.status == STATUS_RUNNING)


def _wider_windows(session: Session, horizon: timedelta, filters: list[Any], now: datetime) -> list[WindowCount]:
    """Each window wider than `horizon`, counted under `filters` (the request's own, minus its window) in one
    query."""
    wider = [(key, span) for key, span in WINDOWS.items() if span is None or span > horizon]
    counts = session.execute(
        select(*(
            func.count() if span is None else func.count().filter(_window_clause(span, now))
            for _, span in wider
        ))
        .select_from(AgentSession)
        .join(Node, Node.id == AgentSession.node_id, isouter=True)
        .where(*filters)
    ).one()
    return [WindowCount(window=key, total=int(count or 0)) for (key, _), count in zip(wider, counts, strict=True)]


def _stamped_cause(cause: str) -> Any:
    """`end_detail` reads `"<cause>: <words>"`, words not blank: `shown_ending`'s prefix test, in SQL."""
    prefix = f"{cause}: "
    # Coalesced, so a NULL detail is a plain false and `not_` of it keeps the row.
    detail = func.coalesce(AgentSession.end_detail, "")
    return and_(detail.startswith(prefix, autoescape=True), func.length(func.trim(detail)) > len(prefix))


def _shown_end_reason_clause(reason: str) -> Any:
    """Rows whose SHOWN `end_reason` (`agent_sessions.shown_ending`) is `reason`, not merely the stored one: a
    break cause stamped onto an inferred ending names the row, so filtering `model_refused` finds the run the CP
    first ended `abandoned`, and filtering `abandoned` leaves it out."""
    from app.runner_causes import CAUSES

    inferred = inferred_ends()
    restamped = and_(AgentSession.end_reason.in_(inferred), or_(*(_stamped_cause(cause) for cause in CAUSES)))
    stored = and_(AgentSession.end_reason == reason, not_(restamped)) if reason in inferred else (
        AgentSession.end_reason == reason
    )
    if reason not in CAUSES:
        return stored
    return or_(stored, and_(AgentSession.end_reason.in_(inferred), _stamped_cause(reason)))


def _ordering(sort: Optional[str], direction: str) -> list[Any]:
    """The ORDER BY, always total.

    By default running sessions first, then most recent. `id` (uuidv7) closes every ordering so offset
    pagination never repeats or skips a row."""
    if sort is None:
        return [
            # `false` sorts before `true`: running rows on top.
            (AgentSession.status != STATUS_RUNNING).asc(),
            AgentSession.started_at.desc(),
            AgentSession.id.desc(),
        ]
    column = SORTS[sort]
    lead = column.asc() if direction == "asc" else column.desc()
    return [lead.nullslast(), AgentSession.started_at.desc(), AgentSession.id.desc()]


@router.get("", response_model=SessionListOut)
@feature("sessions", "view")
def list_sessions(
    page: int = Query(default=1, ge=1, le=_PAGE_MAX),
    per: int = Query(default=PER_PAGE, ge=1, le=_PER_MAX),
    window: str = Query(default=DEFAULT_WINDOW),
    since: Optional[datetime] = Query(
        default=None, description="Started at or after; ISO-8601 with an explicit UTC offset."
    ),
    until: Optional[datetime] = Query(
        default=None, description="Started at or before; ISO-8601 with an explicit UTC offset."
    ),
    sort: Optional[str] = Query(default=None),
    dir_: str = Query(default="desc", alias="dir", pattern="^(asc|desc)$"),
    status: Optional[str] = Query(default=None),
    transcript_status: Optional[str] = Query(default=None),
    # Its own parameter, not a member of the `transcript_status` vocabulary (see `_list_filters`).
    stale: bool = Query(default=False),
    # The shown `end_reason` (`agent_sessions.shown_ending`), one of `end_reasons()`.
    end_reason: Optional[str] = Query(default=None, max_length=_Q_MAX_LEN),
    node: Optional[str] = Query(default=None, max_length=_Q_MAX_LEN),
    task: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=_Q_MAX_LEN),
    # Only dead runs with a pane-log tail to read (the Node page's list).
    pane_log: bool = Query(default=False),
    # The Node page's Dead runs: a pane-log tail to read, or a runner the CP lost (`dead_run_clause`).
    dead: bool = Query(default=False),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> SessionListOut:
    """One page of the fleet's agentic sessions, plus everything the page needs to be honest about it.

    `sessions:view`, not the download's `sessions:edit` (D4): view alone sees rows and downloads none.
    Node-grant scoped like every node-bearing read (`authz.visible_node_ref_clause`; zero grants means
    empty), with the NULL arm (a row naming no node discloses none). Exception: `?stale=true` uses the
    delete's clause (see `_list_filters`). The download rides the same read clause.

    Task titles come through `scoped_task_select`, not a join: a caller reaching no task gets the id
    only, and no join can multiply `total`.
    """
    # The Node page's dead runs list (metadata) reads on every plan; its pane-log tail asks the switch.
    if not (pane_log or dead):
        plan_terms.require_tenant_switch(session, principal, plan_terms.SESSION_TRANSCRIPTS)
    if window not in WINDOWS:
        raise HTTPException(status_code=422, detail=f"unknown window '{window}'")
    since = _require_aware(since, "since")
    until = _require_aware(until, "until")
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="'since' is after 'until'")
    if sort is not None and sort not in SORTS:
        raise HTTPException(status_code=422, detail=f"unknown sort '{sort}'")

    now = datetime.now(timezone.utc)
    filters = _list_filters(
        window=window,
        since=since,
        until=until,
        status=status,
        transcript_status=transcript_status,
        stale=stale,
        end_reason=end_reason,
        node=node,
        task=task,
        q=q,
        # Only the `stale` branch reads it.
        principal=principal,
        now=now,
        pane_log=pane_log,
        dead=dead,
    )

    # The grant clause rides the filter list, reaching count and page identically.
    filters.append(visible_session_clause(principal))

    def filters_but(**dropped: Any) -> list[Any]:
        """The request's filters with some replaced (`end_reason=None`, `window="all"`), the grant kept."""
        kept: dict[str, Any] = dict(
            window=window, since=since, until=until, status=status, transcript_status=transcript_status,
            stale=stale, end_reason=end_reason, node=node, task=task, q=q, principal=principal, now=now,
            pane_log=pane_log, dead=dead,
        )
        return [*_list_filters(**{**kept, **dropped}), visible_session_clause(principal)]

    # The End reason counts see every filter but their own, so a chosen reason never hides the others.
    reason_filters = filters if end_reason is None else filters_but(end_reason=None)
    # Likewise the Node choices: every filter but `node`.
    node_filters = filters if not node else filters_but(node=None)

    # One LEFT join, because `node` is filterable/sortable by name; a node-less session is a real row,
    # and `Node.id` is unique so nothing multiplies.
    def joined(statement: Any) -> Any:
        return statement.join(Node, Node.id == AgentSession.node_id, isouter=True)

    total = session.scalar(
        joined(select(func.count()).select_from(AgentSession)).where(*filters)
    ) or 0
    # Unfiltered except for the grant: "does this viewer have any sessions at all". An unbounded
    # COUNT on every refetch; computing it only when `total` is zero was rejected because the field
    # would then mean two things. Deferred: an approximate count or an honestly named field.
    total_unfiltered = session.scalar(
        select(func.count())
        .select_from(AgentSession)
        .where(visible_session_clause(principal))
    ) or 0
    # A bounded window (no custom range) that holds next to nothing: say what the wider ones hold.
    window_span = WINDOWS[window]
    wider_windows = (
        _wider_windows(session, window_span, filters_but(window="all"), now)
        if window_span is not None and since is None and until is None and total <= _WIDER_HINT_AT_MOST
        and total_unfiltered > total
        else []
    )

    # `held_live` is a selected column (`sessions_cleanup._cp_holds_it_live`, not re-spelled), so the
    # disabled button and the refusing predicate agree. Correlated; page query only, bounded by `per`.
    rows = session.execute(
        joined(select(AgentSession, Node.name, sessions_cleanup._cp_holds_it_live()))
        .where(*filters)
        .order_by(*_ordering(sort, dir_))
        .offset((page - 1) * per)
        .limit(per)
    ).all()

    # One query for every actor name on the page (`app/actors.py`).
    actors = resolve_actor_names(
        session,
        [row.created_by_id for row, *_ in rows] + [row.updated_by_id for row, *_ in rows],
    )

    # Titles via `scoped_task_select`, the only sanctioned path to `visible_task_clause` (a hand-written
    # variant can become an allow-all CROSS JOIN). One bounded query; unreadable tasks get no title.
    wanted_tasks = {row.task_id for row, *_ in rows if row.task_id is not None}
    task_titles: dict[int, str] = {}
    # `(state, since)`: since is the state-transition watermark (`Task.github_synced_at`), else the last write.
    task_states: dict[int, tuple[str, Optional[datetime]]] = {}
    task_metrics: dict[int, Optional[dict]] = {}
    if wanted_tasks:
        for task_row in session.scalars(scoped_task_select(principal).where(Task.id.in_(wanted_tasks))).all():
            task_titles[task_row.id] = task_row.title
            task_states[task_row.id] = (task_row.state, task_row.github_synced_at or task_row.updated_at)
            task_metrics[task_row.id] = task_row.metrics
    costs = listed_session_costs(session, [(row.agent_session_id, row.task_id) for row, *_ in rows], task_metrics)
    # Account names scoped like the roster: an account the caller may not read leaves its rows' cell empty.
    wanted_accounts = {row.account_id for row, *_ in rows if row.account_id is not None}
    account_names: dict[UUID, str] = {}
    if wanted_accounts:
        account_names = dict(session.execute(
            select(Account.id, Account.name)
            .where(Account.id.in_(wanted_accounts), readable_account_clause(principal))
        ).all())

    # The caller's explicit range, kept for the unsessioned attempts: `since` is re-bound to the window below.
    range_since = since
    horizon = WINDOWS[window]
    since = None if horizon is None else now - horizon
    orphan_counts = sorted(
        reconstructed_by_node(session, since, principal=principal), key=lambda pair: -pair[1]
    )
    # Node names scoped like the rows (`visible_node_clause`: `Node.id` is a PK, so no NULL arm), and
    # read only when there is an orphan banner to name nodes for.
    node_names: dict[UUID, str] = {}
    if orphan_counts:
        node_names = {
            node_id: name
            for node_id, name in session.execute(
                select(Node.id, Node.name).where(visible_node_clause(principal))
            ).all()
        }
    orphans = [
        OrphanCount(
            node=node_names.get(node_id) if node_id else None,
            node_id=str(node_id) if node_id and node_id in node_names else None, count=count,
        )
        for node_id, count in orphan_counts
    ]

    # A break report's cause names an ending the CP could only call `abandoned` (`agent_sessions.shown_ending`).
    endings = {row.id: shown_ending(row.end_reason, row.end_detail) for row, *_ in rows}

    # The node lines for the page's unexplained ends, one read of just their nodes (`node_log_history.end_lines`).
    explained = [row for row, *_ in rows
                 if row.node_id is not None and endings[row.id][0] in node_log_history.NODE_EXPLAINED_ENDS]
    end_nodes: dict[UUID, Any] = {}
    if explained:
        end_nodes = {
            node.id: node
            for node in session.execute(
                select(Node.id, Node.org_id, Node.recent_errors)
                .where(Node.id.in_({row.node_id for row in explained}))
            ).all()
        }
    node_errors: dict[UUID, list[str]] = {}
    for row in explained:
        end_node = end_nodes.get(row.node_id)
        if end_node is not None:
            node_errors[row.id] = node_log_history.end_lines(
                node_org=end_node.org_id, recent_errors=end_node.recent_errors, owner_org=row.org_id,
                start=row.started_at, end=row.ended_at,
            )

    vm_failures = _vm_failures(session, [row for row, *_ in rows])
    attempts_over = _attempts_over(session, principal, [row for row, *_ in rows], task_states, now)
    node_statuses = _owing_node_statuses(session, [row for row, *_ in rows])

    storage_available, storage_reason = _storage_state()

    # Skipped when `total_unfiltered` is zero (stale is a subset). Not memoized: the bulk delete's event
    # must refetch a fresh count. Uses the request's own `now`, so it agrees with the `stale` filter.
    stale_count = sessions_cleanup.count_stale(session, principal, now) if total_unfiltered else 0

    return SessionListOut(
        items=[
            SessionOut(
                id=row.id,
                name=row.name,
                agent_session_id=row.agent_session_id,
                agent_kind=row.agent_kind,
                agent_ref=AgentRefOut(**row.agent_ref) if row.agent_ref else None,
                engine=engine_label(row.agent_kind),
                runtime=row.runtime,
                dispatched_runtime=row.dispatched_runtime,
                node=node_name,
                node_id=str(row.node_id) if row.node_id is not None else None,
                account=account_names.get(row.account_id) if row.account_id else None,
                task_id=row.task_id,
                task_title=task_titles.get(row.task_id) if row.task_id else None,
                worktree_ref=row.worktree_ref,
                branch=row.branch,
                holder=row.holder,
                status=row.status,
                end_reason=endings[row.id][0],
                end_reason_label=end_reason_label(endings[row.id][0]),
                started_at=row.started_at,
                ended_at=row.ended_at,
                model=row.model,
                tokens_in=row.tokens_in,
                tokens_out=row.tokens_out,
                cost_usd=costs.get(row.agent_session_id),
                subagent_count=row.subagent_count,
                transcript_status=row.transcript_status,
                transcript_error=row.transcript_error,
                transcript_bytes=row.transcript_bytes,
                transcript_attempts=row.transcript_attempts,
                transcript_leased_at=row.transcript_leased_at,
                held_live=bool(held_live),
                node_errors=node_errors.get(row.id, []),
                pane_log_state=pane_logs.state(row.pane_log_captured_at, row.pane_log_tail),
                end_detail=endings[row.id][1],
                events_last_seq=row.events_last_seq,
                events_refused=row.events_refused,
                no_events=_no_events(row),
                vm_failure=vm_failures.get(row.id),
                attempt_over=attempts_over.get(row.id),
                node_status=node_statuses.get(row.id),
                created_by=actors.get(row.created_by_id),
                updated_by=actors.get(row.updated_by_id),
            )
            for row, node_name, held_live in rows
        ],
        total=total,
        page=page,
        per=per,
        total_unfiltered=total_unfiltered,
        window=window,
        storage_available=storage_available,
        storage_reason=storage_reason,
        redaction_confidence=transcripts.REDACTION_CONFIDENCE,
        overdue_after_seconds=int(STALE_TRANSCRIPT_AFTER.total_seconds()),
        stale_after_seconds=int(sessions_cleanup.stale_after().total_seconds()),
        max_per_call=sessions_cleanup.MAX_DELETE_PER_CALL,
        stale_count=stale_count,
        orphans=orphans,
        end_reasons=_end_reason_options(session, reason_filters),
        node_options=_node_options(session, node_filters),
        wider_windows=wider_windows,
        unsessioned_attempts=(
            _unsessioned_attempts(session, principal, horizon=horizon, since=range_since, until=until, task=task,
                                  now=now)
            if page == 1 and status is None and transcript_status is None and end_reason is None and not stale
            and not node and not q
            else []
        ),
    )


def dead_run_clause() -> Any:
    """A dead run, as the Node page lists them: one whose node sent a pane-log tail, or one the CP lost
    (`runner_terminations.RUNNER_LOST_CAUSES`, the set the relaunch-storm finding counts) — tail or not, so a
    storm on Health always has its runs listed beside it."""
    return or_(
        AgentSession.pane_log_tail.is_not(None),
        AgentSession.end_reason.in_(sorted(runner_terminations.RUNNER_LOST_CAUSES)),
    )


# How long before a session's start its task's VM may have been created and still be the one it ran in.
_VM_LEAD = timedelta(minutes=10)


def _vm_failures(session: Session, rows: list[AgentSession]) -> dict[UUID, str]:
    """`{session row id: the VM's failure sentence}` for the page's ended task sessions whose MicroVM `failed`: the
    task's newest VM created between `_VM_LEAD` before the session started and its end, when that VM is a failed
    one (a relaunch's healthy VM after a failed one is the run's, so it says nothing). Redacted as
    `microvm_lifecycle.failure_of` does; one query, bounded to the page's tasks and time span."""
    if not edition_features.enabled("microvm"):
        return {}  # The community edition has no MicroVMs.
    from app.microvm_placement import FAILED as VM_FAILED

    ended = [row for row in rows if row.task_id is not None and row.status != STATUS_RUNNING]
    if not ended:
        return {}
    vms = session.execute(
        select(Microvm.task_id, Microvm.created_at, Microvm.state, Microvm.wait_reason)
        .where(
            Microvm.task_id.in_({row.task_id for row in ended}),
            Microvm.created_at >= min(row.started_at for row in ended) - _VM_LEAD,
            Microvm.created_at <= max(row.ended_at or row.started_at for row in ended),
        )
        .order_by(Microvm.created_at.desc())
    ).all()
    out: dict[UUID, str] = {}
    for row in ended:
        end = row.ended_at or row.started_at
        newest = next(
            (vm for vm in vms if vm.task_id == row.task_id and row.started_at - _VM_LEAD <= vm.created_at <= end), None
        )
        if newest is not None and newest.state == VM_FAILED:
            out[row.id] = secret_redact.redact((newest.wait_reason or "").strip())[0] or "The MicroVM failed"
    return out


def _attempts_over(
    session: Session, principal: Principal | None, rows: list[AgentSession],
    task_states: dict[int, tuple[str, Optional[datetime]]], now: datetime,
) -> dict[UUID, str]:
    """`{session row id: why its attempt is over}` for the page's `running` task sessions that no end reached: the
    task left a runner's states (`task_lifecycle._RUNNER_RUNNING_STATES`), or a later session of the same task
    started (the relaunch replaced it) — each at least `_ATTEMPT_OVER_GRACE` ago, so a settle whose session end is
    still on its way is not called unreported. Read-time only — the row stays `running` in the table, as the node
    left it; the page shows it ended. `task_states` is the page's readable tasks (`scoped_task_select`); one query
    for the later sessions, under the same grant clause as the rows."""
    running = [row for row in rows if row.task_id is not None and row.status == STATUS_RUNNING]
    if not running:
        return {}
    newest = dict(session.execute(
        select(AgentSession.task_id, func.max(AgentSession.started_at))
        .where(AgentSession.task_id.in_({row.task_id for row in running}), visible_session_clause(principal))
        .group_by(AgentSession.task_id)
    ).all())
    out: dict[UUID, str] = {}
    for row in running:
        task_id = row.task_id
        if task_id is None:
            continue
        state, since = task_states.get(task_id, (None, None))
        settled = since is None or now - since >= _ATTEMPT_OVER_GRACE
        if state is not None and state not in task_lifecycle._RUNNER_RUNNING_STATES and settled:
            words = _TASK_STATE_WORDS.get(state, state.replace("_", " "))
            out[row.id] = f"The task is {words}, so this run is over; its node never reported the end"
        elif (later := newest.get(task_id)) is not None and row.started_at < later <= now - _ATTEMPT_OVER_GRACE:
            out[row.id] = "A later run of this task replaced it; its node never reported the end"
    return out


def _attempt_over_clause(principal: Principal | None, now: datetime) -> Any:
    """`_attempts_over` as SQL, for the status filter: a task session whose readable task left a runner's states, or
    whose task's newest visible session started after it, each at least `_ATTEMPT_OVER_GRACE` ago. Correlates to the
    enclosing `select(AgentSession)`; false for a session with no task, never NULL."""
    cutoff = now - _ATTEMPT_OVER_GRACE
    since = func.coalesce(Task.github_synced_at, Task.updated_at)
    task_left = AgentSession.task_id.in_(
        scoped_task_select(principal)
        .where(Task.state.not_in(sorted(task_lifecycle._RUNNER_RUNNING_STATES)), or_(since.is_(None), since <= cutoff))
        .with_only_columns(Task.id)
    )
    newest = (
        select(AgentSession.task_id.label("task_id"), func.max(AgentSession.started_at).label("started_at"))
        .where(
            AgentSession.task_id.is_not(None), visible_session_clause(principal),
            # Only tasks with a running row can make one over: bounds the grouping to them.
            AgentSession.task_id.in_(
                select(AgentSession.task_id).where(AgentSession.status == STATUS_RUNNING)
            ),
        )
        .group_by(AgentSession.task_id)
        .subquery()
    )
    replaced = exists().where(
        newest.c.task_id == AgentSession.task_id,
        newest.c.started_at > AgentSession.started_at,
        newest.c.started_at <= cutoff,
    )
    return and_(AgentSession.task_id.is_not(None), or_(task_left, replaced))


# How long after the task leaves a runner's states (or a later run starts) a still-`running` row reads over: a
# settle's own session end lands within seconds, so only one that never came is called unreported.
_ATTEMPT_OVER_GRACE = timedelta(minutes=5)


# A task state as `_attempts_over`'s sentence words it ("The task is …").
_TASK_STATE_WORDS: dict[str, str] = {
    task_lifecycle.DONE: "done",
    task_lifecycle.SYSTEM_FAILED: "failed",
    task_lifecycle.PAUSED: "paused",
    task_lifecycle.READY: "back in the queue",
    task_lifecycle.DRAFT: "a draft again",
}


def _owing_node_statuses(session: Session, rows: list[AgentSession]) -> dict[UUID, str]:
    """`{session row id: its node's presence}` for the page's rows still owing a transcript (`pending`/`uploading`):
    the upload is the node's, so an offline node is why it waits. One presence read, only when a row owes."""
    owing = [row for row in rows
             if row.node_id is not None and row.transcript_status in (TRANSCRIPT_PENDING, TRANSCRIPT_UPLOADING)]
    if not owing:
        return {}
    presence = jaah_discovery.presence_map(session)
    last_seen = dict(session.execute(
        select(Node.id, Node.last_seen).where(Node.id.in_({row.node_id for row in owing}))
    ).all())
    return {
        row.id: jaah_discovery.status_and_last_seen(presence.get(str(row.node_id)), last_seen.get(row.node_id))[0]
        for row in owing
    }


def _no_events(row: AgentSession) -> bool:
    """An ended session that was `acp` — by its stream or by its dispatch — yet stored no event. The dispatch arm
    is the point: `runtime` turns `acp` only on the first stored batch, so a launch that died first is `legacy`."""
    return (
        row.status != STATUS_RUNNING
        and RUNTIME_ACP in (row.runtime, row.dispatched_runtime)
        and (row.events_last_seq or 0) == 0
    )


# The unsessioned-attempts banner's ceiling: the newest this many, so the list costs one bounded query.
_UNSESSIONED_MAX = 50
# How far back a task's FIRST termination looks for its session, absent an earlier termination to bound it.
_UNSESSIONED_LOOKBACK = timedelta(hours=24)
# The oldest note the banner reads, whatever the window (`all` included): each note runs a correlated session
# probe, so an unbounded scan of `activity_events` would grow with the org's whole history.
_UNSESSIONED_MAX_AGE = timedelta(days=7)


def _unsessioned_attempts(
    session: Session,
    principal: Principal | None,
    *,
    horizon: Optional[timedelta],
    since: Optional[datetime],
    until: Optional[datetime],
    task: Optional[int],
    now: datetime,
) -> list[UnsessionedAttemptOut]:
    """Task attempts that ended with no session row, newest first.

    An attempt is a `task_note:runner_terminated` note; it had a session when one of its task's rows started
    after that task's previous termination (or `_UNSESSIONED_LOOKBACK` before, for its first) and by the note.
    Windowed on the note's `created_at` as the list windows `started_at`, and never older than
    `_UNSESSIONED_MAX_AGE` even for `window=all`. Task ids compare as TEXT: `activity_events.data.id` is a uuid on
    other kinds, and an integer cast in WHERE would fail on one before the kind filter applies. Scoped twice: the read seam's org
    predicate on `activity_events`, and the task's readability (`scoped_task_select`) — a note about a task this
    viewer cannot read is not served, title or no title."""
    kind = f"task_note:{runner_terminations.NOTE_ACTION}"
    task_ref = ActivityEvent.data["id"].astext
    earlier = aliased(ActivityEvent)
    previous_at = (
        select(func.max(earlier.created_at))
        .where(
            earlier.kind == kind,
            earlier.data["id"].astext == task_ref,
            earlier.created_at < ActivityEvent.created_at,
        )
        # Explicit: nested in the EXISTS below, auto-correlation would stop at the `sessions` level and cross-join
        # a fresh `activity_events` (any newer event anywhere would then read as this task's previous termination).
        .correlate(ActivityEvent)
        .scalar_subquery()
    )
    had_session = exists().where(
        sa_cast(AgentSession.task_id, Text) == task_ref,
        AgentSession.started_at > func.coalesce(previous_at, ActivityEvent.created_at - _UNSESSIONED_LOOKBACK),
        AgentSession.started_at <= ActivityEvent.created_at,
    )
    filters: list[Any] = [
        ActivityEvent.kind == kind,
        task_ref.in_(scoped_task_select(principal).with_only_columns(sa_cast(Task.id, Text))),
        ~had_session,
        ActivityEvent.created_at >= now - _UNSESSIONED_MAX_AGE,
    ]
    if horizon is not None:
        filters.append(ActivityEvent.created_at >= now - horizon)
    if since is not None:
        filters.append(ActivityEvent.created_at >= since)
    if until is not None:
        filters.append(ActivityEvent.created_at <= until)
    if task is not None:
        filters.append(task_ref == str(task))
    notes = session.execute(
        select(ActivityEvent.created_at, ActivityEvent.data)
        .where(*filters)
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(_UNSESSIONED_MAX)
    ).all()
    if not notes:
        return []

    task_ids = {int(data["id"]) for _, data in notes}
    titles = {
        row.id: row.title
        for row in session.scalars(scoped_task_select(principal).where(Task.id.in_(task_ids))).all()
    }
    # `data.node` is a uuid string (`runner_terminations.record`), parsed fail-soft: a bad one names no node.
    node_ids = {optional_uuid(data.get("node")) for _, data in notes} - {None}
    node_names: dict[UUID, str] = {}
    if node_ids:
        # Scoped like the rows' node names: a node this viewer does not reach is not named.
        node_names = dict(session.execute(
            select(Node.id, Node.name).where(Node.id.in_(node_ids), visible_node_clause(principal))
        ).all())
    return [
        UnsessionedAttemptOut(
            task_id=int(data["id"]),
            task_title=titles.get(int(data["id"])),
            at=at,
            cause=data.get("cause") if isinstance(data.get("cause"), str) else None,
            label=runner_terminations.label_for(data.get("cause")),
            detail=data.get(runner_terminations.DETAIL_KEY) or None,
            node=node_names.get(node_id) if (node_id := optional_uuid(data.get("node"))) else None,
        )
        for at, data in notes
    ]


# ── The operator's prune ──────────────────────────────────────────────────────────────────────────
# Needed because a `sessions` row is written before the runner exists, so a never-started spawn leaves
# a row nothing can finish. Staleness, its knob, the S3 guard and the backlog live in
# `app/sessions_cleanup.py`; both routes fire its identical predicate over one clock. Both are
# `sessions:edit` (it changes what the fleet recorded); `_why_protected` only words a SQL refusal.


class StaleDeleteOut(BaseModel):
    """What one press of the bulk button actually did — never merely that it succeeded."""

    deleted: int
    # Whether the ceiling bit, so the page can say "click again" rather than claim success. Needs both
    # `deleted` and `remaining` (see the handler); named to match the page.
    truncated: bool
    # The ceiling itself, so the page owns no copy.
    max_per_call: int
    # Re-counted after the delete, never subtracted: rows cross the threshold and others may click.
    remaining: int


@router.post("/stale/delete", response_model=StaleDeleteOut)
@feature("sessions", "edit")
def delete_stale_sessions(
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> StaleDeleteOut:
    """Delete every stale session row this principal can see — the button under the list's banner.

    Declared before `DELETE /{session_id}` so the literal path cannot be swallowed by the parameter.
    Node-grant scoped like the list and the single-row delete, so a confined holder cannot destroy
    rows they cannot see. The count and the delete get the identical principal — the whole contract.
    Bounded by `sessions_cleanup.MAX_DELETE_PER_CALL`, reported via `truncated` + `remaining`.
    """
    # One clock for sweep and re-count, so `deleted + remaining` describes one instant.
    now = datetime.now(timezone.utc)
    deleted = sessions_cleanup.delete_stale(session, principal, now)
    if deleted:
        # Only when rows went, and before the commit (`pg_notify` is transactional). `id=0` is the
        # collection sentinel (a real id would reach subscribers who cannot read the list), tagged with
        # the caller's org (`routes/stream.py::_may_see_collection_sentinel`).
        emit_console_event(
            session, entity="session", id=0, action="changed", org_id=user.org_id,
        )
    session.commit()
    if deleted:
        # The actor is logged because the rows take their `updated_by_id` with them.
        log.info("sessions: %s deleted %d stale session row(s)", user.email, deleted)
    # Re-counted before `truncated` is decided, with the same principal and clock.
    remaining = sessions_cleanup.count_stale(session, principal, now)
    return StaleDeleteOut(
        deleted=deleted,
        # `and remaining > 0`: the flag is about what is left, so an exact-ceiling pass that cleared the
        # backlog is not truncated.
        truncated=deleted >= sessions_cleanup.MAX_DELETE_PER_CALL and remaining > 0,
        max_per_call=sessions_cleanup.MAX_DELETE_PER_CALL,
        remaining=remaining,
    )


@router.delete("/{session_id}", status_code=204)
@feature("sessions", "edit")
def delete_session(
    session_id: UUID,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Delete one stale session row the operator is looking at.

    The same predicate as the bulk button (`stale_clause` + `deletable_node_ref_clause`), shared with
    `count_stale` and `?stale=true`, so the wire agrees with the affordance. The liveness conjunct
    protects a running session (age alone cannot: advisor chats and `awaiting_response` runners run
    arbitrarily long); `_why_protected` words the refusal. The delete's clause drops the NULL arm, since
    retiring a node would otherwise expose its history to zero-grant holders.

    404 for everything this principal may not destroy, and order matters: scope is applied first, so an
    out-of-scope protected row answers 404, not a 409 that would confirm it exists. A test pins this.
    """
    # One clock for the delete and any refusal it explains.
    now = datetime.now(timezone.utc)
    cutoff = sessions_cleanup.stale_cutoff(now)

    # The predicate is in the delete, not checked in Python first (a concurrent upload could move the
    # row between statements), and on the target relation too: EvalPlanQual rechecks only the target's
    # own qualifiers; the `IN (SELECT …)` reruns under the original snapshot. `stale_clause` is passed
    # whole, never a partial copy. The grant clause stays in the subquery, as in `delete_stale`: off a
    # bare `delete()` it may render uncorrelated (allow-all).
    targeted = (
        select(AgentSession.id)
        .where(AgentSession.id == session_id)
        .where(sessions_cleanup.stale_clause(cutoff))
        .where(sessions_cleanup.deletable_node_ref_clause(principal, AgentSession.node_id))
    )
    # RETURNING: the identifiers are the only record the row existed; reading them first would reopen
    # the race.
    removed = session.execute(
        delete(AgentSession)
        .where(AgentSession.id.in_(targeted))
        .where(sessions_cleanup.stale_clause(cutoff))
        .returning(
            AgentSession.agent_session_id,
            AgentSession.name,
            AgentSession.status,
            AgentSession.transcript_status,
        ),
        execution_options={"synchronize_session": False},
    ).first()
    if removed is None:
        _refuse_single_delete(session, session_id, principal, cutoff)

    # The actor is logged because the row takes its `updated_by_id` with it.
    log.info(
        "sessions: %s deleted session %s (%r, status=%s, transcript_status=%s)",
        user.email, removed.agent_session_id, removed.name, removed.status, removed.transcript_status,
    )
    # Same sentinel, pre-commit placement and org tag as the bulk route.
    emit_console_event(
        session, entity="session", id=0, action="changed", org_id=user.org_id,
    )
    session.commit()
    return Response(status_code=204)


def _refuse_single_delete(
    session: Session, session_id: UUID, principal: Principal | None, cutoff: datetime
) -> NoReturn:
    """Why the delete matched nothing — 404 if it is gone or out of scope, 409 if it is protected.

    Always raises. The lookup applies the scope clause, so an out-of-scope row never reaches the state
    inspection and can only answer 404.
    """
    # Gone or unreachable, deliberately indistinguishable.
    row = get_or_404(
        session, AgentSession, session_id, detail="no such session",
        where=(sessions_cleanup.deletable_node_ref_clause(principal, AgentSession.node_id),),
    )
    # The one conjunct not readable off the row (liveness), fired from `sessions_cleanup`'s own clause
    # rather than re-spelled. One bounded correlated read.
    cp_holds_it_live = bool(session.scalar(
        select(sessions_cleanup._cp_holds_it_live())
        .select_from(AgentSession)
        .where(AgentSession.id == row.id)
    ))
    raise HTTPException(status_code=409, detail=_why_protected(row, cutoff, cp_holds_it_live))


# Statuses reachable only through an upload ticket, hence possibly naming bucket objects. Not
# `unavailable`/`not_applicable`: a node reports those without a ticket
# (`apps/jaahd/session_sweep.py`).
_TICKETED_TRANSCRIPT_STATUSES: frozenset[str] = frozenset({
    TRANSCRIPT_UPLOADING, TRANSCRIPT_UPLOADED, TRANSCRIPT_FAILED, TRANSCRIPT_GAVE_UP,
})


def _why_protected(row: AgentSession, cutoff: datetime, cp_holds_it_live: bool) -> str:
    """The 409's sentence, naming which conjunct of `stale_clause` the row failed.

    Not a second predicate: the SQL delete already decided; this only words it, with a catch-all last
    branch that can never let a row through. `cp_holds_it_live` is the liveness conjunct, supplied by
    the caller.

    Ordered ticketed → settled → ended → live → young, since only one sentence is read: possible S3
    bytes first (irreversible), then settled/queued transcripts (`ended` + still-held is ordinary),
    then liveness above age (a live run can be arbitrarily old). The page mirrors this order
    (`apps/web/src/pages/audit/SessionsTab.tsx`); change both together.
    """
    if (
        row.transcript_status in _TICKETED_TRANSCRIPT_STATUSES
        or row.transcript_attempts
        or row.transcript_leased_at is not None
    ):
        # Members are PUT one at a time and the manifest lands last, so ticketed rows may hold objects;
        # this row is their only pointer (versioned bucket, no expiration).
        return (
            "this session has been issued an upload ticket "
            f"(transcript_status={row.transcript_status}, attempts={row.transcript_attempts}), so it "
            "may already have transcript members in object storage; this row is the only pointer to "
            "them, and deleting it would strand those objects with nothing left to find them by"
        )
    if row.transcript_status != TRANSCRIPT_PENDING:
        # `unavailable`/`not_applicable` with no ticket: finished audit records, never ticketed.
        return (
            f"this session's transcript is settled (transcript_status={row.transcript_status}) and "
            "the row is a completed audit record; the prune only removes rows that are STUCK — a "
            "dispatched run that never started — never one that finished"
        )
    if row.status != STATUS_RUNNING:
        return (
            f"this session has ended (status={row.status}) and its transcript is collected by the "
            "ordinary upload queue; the prune only removes rows nothing in the system can ever finish"
        )
    if cp_holds_it_live:
        # Something is still inside it: deleting would lose the record of a running process, its
        # bindings, and its ending (`POST /{sid}/end` would 404).
        return (
            "this session is still held as LIVE — a runner is inside it (an open task's "
            "runner_session_id) or it is the session occupying a pool slot — so a runner may be "
            "executing inside it right now; it is not stuck and no prune will ever take it. Deleting "
            "the row would leave that runner reporting against a session that no longer exists, and "
            "its task, account and cost bindings would be lost. It becomes deletable on its own once "
            "the run settles"
        )
    if row.started_at >= cutoff:
        hours = settings_reads.get_int(sessions_cleanup.STALE_SESSION_AFTER_HOURS_KEY)
        return (
            f"this session started less than {hours}h ago, inside the staleness threshold — it may be "
            "a run a node is executing right now, and deleting its record would leave that node "
            "reporting against a session that no longer exists"
        )
    # The race: not stale at DELETE time, stale by this re-read. Also the safe fallback for a conjunct
    # this function has not learned (a vague 409, never a 204).
    return (
        "this session is not in the set the prune removes; reload the list to see its current state"
    )
