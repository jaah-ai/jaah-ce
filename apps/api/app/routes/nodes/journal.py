"""`GET /api/nodes/{node_id}/journal` — the console's read of a node's shipped systemd journal.

Every Linux node ships its journal (`apps/jaahd/node_logs.py` → `POST /api/agent/node-logs` → CloudWatch,
group `config.node_log_group()`, stream = node name); this is the one place the console reads it back, so a
dead daemon is diagnosed in the UI, not with AWS tools. Operator only: a node journal is infrastructure.
Vnode daemons ship nothing today (`node_logs.py` header), so a virtual node's stream is empty.

The stream is the node's ID (`ship_node_logs` writes it): a name is renameable and unique per org only, so a
name-keyed stream let a tenant forge another host's lines. History shipped before that change sits in a stream
keyed by NAME; it is read as a fallback when the id stream is empty and flagged `stream: "name"`, since its lines
may belong to any node that ever had the name (a known limit, not fixed here).

Never a 5xx on AWS's account: an unconfigured group or a failed read is a 200 with `error` and no entries.
`before`/`after` bound the window: in incident forensics the lines that matter are hours old, and a busy host ships
300 lines in 20 minutes, so the newest page alone never reaches them.

Message text is scrubbed (`fleet_verdicts.redact_credentials`) before the clip, as `cp_ecs.task_output` does."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config, edition_features, fleet_verdicts
from app.auth import require_operator
from app.authz.catalog import Tenancy
from app.authz.declarations import superadmin_only
from app.db import get_session
from app.models.identity import AdminUser
from app.routes.nodes.reads import _node_row

router = APIRouter(tags=["nodes"])

_NOT_CONFIGURED = "node log shipping is not configured on this control plane"

_LINE_LIMIT = 2000
_DEFAULT_PRIORITY = 6  # info: what a line that is not the shipper's JSON is taken to be


class NodeJournalEntryOut(BaseModel):
    ts: datetime
    unit: str
    priority: int
    message: str


class NodeJournalOut(BaseModel):
    entries: list[NodeJournalEntryOut]
    error: str | None
    group_configured: bool
    stream: Literal["id", "name"] = "id"
    before: datetime | None = None


def _utc_ms(moment: datetime | None) -> int | None:
    """Epoch ms of an ISO-8601 moment; a naive one is UTC."""
    if moment is None:
        return None
    return int(moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp() * 1000)


def _entry(event: dict[str, object]) -> NodeJournalEntryOut | None:
    """One CloudWatch event as an entry: the shipper's `{unit, priority, message}` JSON, or the raw text whole."""
    raw = event.get("message")
    timestamp = event.get("timestamp")
    if not isinstance(raw, str) or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return None
    unit, priority, text = "", _DEFAULT_PRIORITY, raw
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        unit = str(parsed.get("unit", ""))
        shipped_priority = parsed.get("priority")
        if isinstance(shipped_priority, int) and not isinstance(shipped_priority, bool):
            priority = shipped_priority
        text = str(parsed.get("message", ""))
    try:
        ts = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return NodeJournalEntryOut(
        ts=ts,
        unit=unit,
        priority=priority,
        # Scrubbed before the cut, so a token the cut would split is still whole when it is matched.
        message=fleet_verdicts.redact_credentials(text.rstrip("\n")[: 2 * _LINE_LIMIT])[:_LINE_LIMIT],
    )


def _read(
    group: str, stream: str, limit: int, start_ms: int | None, end_ms: int | None,
) -> list[dict[str, object]]:
    """The stream's newest events; a stream that does not exist (nothing shipped under it) is none."""
    if not edition_features.enabled("node_log_archive"):
        return []
    from app.aws import cloudwatch_logs

    try:
        return cloudwatch_logs.get_log_events(group, stream, limit=limit, start_ms=start_ms, end_ms=end_ms)
    except cloudwatch_logs.CloudWatchLogsError as exc:
        if exc.code != cloudwatch_logs.NOT_FOUND:
            raise
        return []


@router.get("/api/nodes/{node_id}/journal", response_model=NodeJournalOut)
@superadmin_only(Tenancy.OPERATOR)
def get_node_journal(
    node_id: str,
    limit: int = Query(300, ge=1, le=1000),
    before: datetime | None = None,
    after: datetime | None = None,
    _: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> NodeJournalOut:
    """The node's newest `limit` shipped journal lines up to `before` (default now) and not older than `after`,
    oldest first. 404 for an unknown node; 422 when `after` is not earlier than `before`."""
    start_ms, end_ms = _utc_ms(after), _utc_ms(before)
    if start_ms is not None and end_ms is not None and start_ms >= end_ms:
        raise HTTPException(status_code=422, detail="'after' must be earlier than 'before'")
    row = _node_row(session, node_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    if not edition_features.enabled("node_log_archive"):
        return NodeJournalOut(entries=[], error=_NOT_CONFIGURED, group_configured=False, before=before)
    from app.aws import json11

    group = config.node_log_group()
    if group is None:
        return NodeJournalOut(entries=[], error=_NOT_CONFIGURED, group_configured=False, before=before)

    stream: Literal["id", "name"] = "id"
    try:
        events = _read(group, str(row.id), limit, start_ms, end_ms)
        if not events:
            events, stream = _read(group, row.name, limit, start_ms, end_ms), "name"
    except (json11.AwsError, ValueError) as exc:
        return NodeJournalOut(entries=[], error=str(exc), group_configured=True, before=before)
    entries = [entry for entry in map(_entry, events) if entry is not None]
    # Nothing under either stream: the id stream is the answer, not a legacy one.
    return NodeJournalOut(
        entries=entries, error=None, group_configured=True, stream=stream if entries else "id",
        before=before,
    )
