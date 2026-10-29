"""The task view's read of an `acp` session's event stream (`app/session_events.py`): the live poll and the download.

  GET /api/tasks/{task_id}/sessions/{agent_session_id}/events?after=<seq>   the next page (the view polls every 1s)
  GET /api/tasks/{task_id}/sessions/{agent_session_id}/events.jsonl         the whole stream as one file

`sessions:edit`, as the transcript download: the stream carries what the agent said and ran. The session must be one
the caller may see (`visible_session_clause`) and belong to a task the caller may see (`scoped_task_select`); any
other pair is the same 404."""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import data_access_events, hipaa
from app import session_transcripts as transcripts
from app import session_events
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_task_select, visible_session_clause
from app.db import get_session
from app.models.agent_sessions import AgentSession
from app.models.identity import AdminUser
from app.models.tasks import Task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class SessionEventsOut(BaseModel):
    session_id: str
    #: The session's own status (`running`/`ended`/`unknown`): the view stops polling once it has ended and a
    #: read returned nothing more.
    status: str
    runtime: str
    events: list[dict[str, Any]]
    #: The highest `seq` in `events`, or `after` when there are none — the next poll's `after`.
    last_seq: int
    #: More events are already stored: ask again at once with `after=last_seq`.
    more: bool


def _session_row(
    session: Session, principal: Optional[Principal], task_id: int, agent_session_id: str
) -> AgentSession:
    if principal is None:
        raise HTTPException(status_code=404, detail="no such session on this task")
    task = session.scalars(scoped_task_select(principal).where(Task.id == task_id)).first()
    row = session.scalars(
        select(AgentSession).where(
            AgentSession.agent_session_id == agent_session_id,
            AgentSession.task_id == task_id,
            visible_session_clause(principal),
        )
    ).first()
    if task is None or row is None:
        raise HTTPException(status_code=404, detail="no such session on this task")
    return row


def _where(row: AgentSession) -> session_events.Where:
    where = session_events.where_for(row)
    if where is None:
        raise HTTPException(status_code=503, detail="Event storage is not configured on this control plane.")
    return where


@router.get("/{task_id}/sessions/{agent_session_id}/events", response_model=SessionEventsOut)
@feature("sessions", "edit")
def read_session_events(
    task_id: int,
    agent_session_id: str,
    request: Request,
    after: int = Query(default=0, ge=0, le=session_events.MAX_SEQ),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> SessionEventsOut:
    """Events with `seq > after`, at most `session_events.READ_LIMIT`. A legacy session answers an empty page:
    it has no stream. In a HIPAA org every page that returns events is a PHI read (a person's unattended
    background poll excepted, as everywhere — `hipaa.stage_phi_read`)."""
    row = _session_row(session, principal, task_id, agent_session_id)
    status, runtime, last_seq, org_id = row.status, row.runtime, row.events_last_seq, row.org_id
    if not last_seq or last_seq <= after:
        events: list[dict[str, Any]] = []
        more = False
    else:
        where = _where(row)
        # Release the connection before the S3 reads; the plain values above are all that is needed.
        session.rollback()
        try:
            events, more = session_events.read(where, last_seq=last_seq, after=after)
        except (session_events.EventStorageUnavailable, transcripts.KeyRefused) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        if events:
            hipaa.record_phi_read(
                session, request, record_type="session_events", record_id=agent_session_id,
                task_id=task_id, org_id=org_id, user=_user,
            )
    return SessionEventsOut(
        session_id=agent_session_id,
        status=status,
        runtime=runtime,
        events=events,
        last_seq=events[-1]["seq"] if events else after,
        more=more,
    )


@router.get(
    "/{task_id}/sessions/{agent_session_id}/events.jsonl", response_class=StreamingResponse, response_model=None
)
@feature("sessions", "edit")
def download_session_events(
    task_id: int,
    agent_session_id: str,
    request: Request,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """The whole stream, one §3.1 event per line — the same shape for every engine. Recorded as a data access
    (and a PHI read in a HIPAA org) before the first byte, as the transcript download is."""
    row = _session_row(session, principal, task_id, agent_session_id)
    if not row.events_last_seq:
        raise HTTPException(status_code=409, detail="This session has no event stream.")
    where = _where(row)
    last_seq = row.events_last_seq
    org_id = row.org_id
    hipaa.stage_phi_read(
        session, request, record_type="session_events", record_id=agent_session_id,
        task_id=task_id, org_id=org_id, on=hipaa.org_is_on(session, org_id),
        user_id=_user.id, actor_label=_user.email,
    )
    data_access_events.record_committed(
        session,
        data_access_events.TRANSCRIPT_DOWNLOADED,
        f"Downloaded the event stream of session {agent_session_id}",
        data={"session_id": str(row.id), "agent_session_id": agent_session_id, "task_id": task_id},
        org_id=org_id,
        user_id=_user.id,
        actor_label=_user.email,
    )

    def lines() -> Iterator[bytes]:
        for event in session_events.iter_all(where, last_seq=last_seq):
            yield (json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            # Sanitised: the id is the node's, and a `"` or `;` would reshape the header.
            "Content-Disposition": f'attachment; filename="session-{re.sub(r"[^A-Za-z0-9._-]", "_", agent_session_id)}-events.jsonl"',
            "Cache-Control": "no-store",
        },
    )
