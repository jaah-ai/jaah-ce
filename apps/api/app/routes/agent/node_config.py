"""Node self-reports and self-reads: config snapshot, egress, logs, model catalogs, disk reclaims, liveness, identity and credential rotation."""
from __future__ import annotations

import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app import (
    agent_providers,
    config,
    edition_features,
    node_config,
    node_credentials,
    node_disk_events,
    node_log_findings,
)
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.clock import as_utc
from app.authz.declarations import infra
from app.db import get_session
from app.events import emit_console_event
from app.node_identity import names_by_id, node_id_or_none, node_matches, wire_node_id_or_404
from app.models.nodes import Node
from app.entity_shape import RefOut
from app.routes.agent.auth import _check_token, caller_node_id, caller_node_ref

if edition_features.enabled("nodelink"):
    from app import nodelinks

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# ── Self-heal markers: a fan-in, not a fan-out ───────────────────────────────────────────────────────


class ConfigSnapshotIn(BaseModel):
    # The name from `/etc/jaah/node.env`; `app/node_config.py` resolves it to the row.
    node_id: str
    # The daemon's allowlist-redacted config; redaction is daemon-side (a pinned contract).
    config: dict[str, Any]
    reported_at: Optional[str] = None
    # A MicroVM node's own report (architecture §4.3), beside `config` because it is not config. Opaque JSON,
    # never refused: kept as `config_snapshot['microvm']` (`node_config.apply_config_snapshot`). Absent = none.
    microvm: Any = None


class ConfigSnapshotOut(BaseModel):
    changed: bool


@router.post("/nodes/config-snapshot", response_model=ConfigSnapshotOut)
@infra(Tenancy.ORG_SCOPED)
def set_node_config_snapshot(
    body: ConfigSnapshotIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> ConfigSnapshotOut:
    """Persist a node's config snapshot. The publish-on-change compare stays node-side. A node may report
    only its own (`caller_node_ref`): the snapshot can correct `nodes.jaah_home`.

    The degradation contract lives in `app/node_config.py`: a bad `reported_at` degrades to NULL without
    dropping the snapshot; the change gate compares `config` only.
    """
    caller_node_ref(session, caller, body.node_id)
    changed = node_config.apply_config_snapshot(
        session, body.node_id, body.config, body.reported_at, microvm=body.microvm
    )
    # The NodeLink half: the accounts root it resolved (bounded) and which NodeLinks it has mounted. The local
    # edition has no NodeLink.
    node = session.scalar(select(Node).where(node_matches(body.node_id)))
    mounted = (
        nodelinks.ingest_snapshot(session, node, body.config) if node is not None and edition_features.enabled("nodelink") else None
    )
    if changed:
        # The event `id` is the uuid, like every `entity="node"` emit in `routes/nodes/`. `str(...)` is
        # load-bearing: a bare `UUID` would make `json.dumps` raise after the write. The `or body.node_id` arm
        # is a future-proof fallback.
        resolved = node_id_or_none(session, body.node_id)
        emit_console_event(
            session, entity="node",
            id=str(resolved) if resolved is not None else body.node_id,
            action="upserted",
        )
    session.commit()
    if node is not None and mounted is not None:
        # After the commit (it takes the store lock and commits); a NodeLink this node no longer reports
        # mounted stops holding its delete. Best effort: the next snapshot retries it.
        try:
            nodelinks.recheck_deletes(session, node, mounted)
        except Exception:  # noqa: BLE001 — never fail a snapshot over a pending delete
            session.rollback()
            log.warning("nodelinks: recheck after %s's snapshot failed", body.node_id, exc_info=True)
    return ConfigSnapshotOut(changed=changed)


# ── Node egress IPv4, for the headscale edge's allowlist ───────────────────────────────────────────


class NodeEgressIn(BaseModel):
    # The node's public IPv4 (`tailscale netcheck` GlobalV4), or null when netcheck found none.
    ipv4: Optional[str] = None
    # Only for a caller with no derived identity (CP loopback, dev bypass); a node's own token decides.
    node_id: Optional[str] = None

    @field_validator("ipv4")
    @classmethod
    def _public_ipv4(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            address = ipaddress.IPv4Address(value.strip())
        except ValueError:
            raise ValueError("ipv4 must be an IPv4 address or null") from None
        if not address.is_global or address.is_multicast:
            raise ValueError("ipv4 must be a public unicast address")
        return str(address)


@router.post("/nodes/egress", status_code=204)
@infra(Tenancy.ORG_SCOPED)
def set_node_egress(
    body: NodeEgressIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Stamp the CALLING node's egress IPv4 and its report time (`app/headscale_allowlist.py` reads both).

    The node is the credential's, never the body's (`caller_node_id`): a node may only report itself.
    """
    node_id = caller_node_id(session, caller, body.node_id or "")
    session.execute(
        update(Node)
        .where(Node.id == node_id)
        .values(egress_ipv4=body.ipv4, egress_ipv4_reported_at=func.now())
    )
    session.commit()
    return Response(status_code=204)


# ── Node journals, shipped to CloudWatch Logs (SOC2 G-18) ──────────────────────────────────────────

# The whole request body, checked on the raw bytes before any JSON parse.
NODE_LOGS_MAX_BODY_BYTES = 1_048_576


# What CloudWatch stores at all (older/newer events are dropped inside a 2xx), applied here so a
# node-chosen spread of timestamps cannot fan one body out into thousands of AWS calls.
NODE_LOGS_MAX_AGE = timedelta(days=14)


NODE_LOGS_MAX_FUTURE = timedelta(hours=2)


# A 14-day backlog is at most 15 day-spans plus a byte split; a body needing more calls is refused.
NODE_LOGS_MAX_CALLS = 32
_NODE_LOGS_NOT_CONFIGURED = "node log shipping is not configured on this control plane"


class NodeLogEntryIn(BaseModel):
    ts: datetime
    unit: str = Field(max_length=256)
    priority: int = Field(ge=0, le=7)
    message: str


class NodeLogsIn(BaseModel):
    # The node's journal cursor after the last entry in this batch; echoed back once all are stored.
    cursor: str = Field(min_length=1, max_length=4096)
    entries: list[NodeLogEntryIn] = Field(max_length=10000)


class NodeLogsOut(BaseModel):
    cursor: str


async def _node_logs_body(
    request: Request, caller: node_credentials.AgentCaller = Depends(_check_token)
) -> NodeLogsIn:
    """The body, bounded at `NODE_LOGS_MAX_BODY_BYTES` on its RAW bytes — the declared length first, then
    the bytes actually read, so a chunked or lying sender is cut off before the whole body is buffered.
    Async dependency, sync handler, as `routes/attachments.py::_artifact_ticket_body`. A node token only:
    an arm that derives no node (CP loopback, dev bypass) has no journal, and is refused before a byte is
    buffered."""
    if caller.node_id is None:
        raise HTTPException(status_code=403, detail="node logs are accepted only on a node's own token")
    oversize = HTTPException(
        status_code=413, detail=f"a node-logs body must be at most {NODE_LOGS_MAX_BODY_BYTES} bytes"
    )
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > NODE_LOGS_MAX_BODY_BYTES:
        raise oversize
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > NODE_LOGS_MAX_BODY_BYTES:
            raise oversize
        chunks.append(chunk)
    try:
        return NodeLogsIn.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        # A count, not `exc.errors()`, which would quote the journal back.
        raise HTTPException(
            status_code=422, detail=f"node-logs body rejected — {exc.error_count()} field(s) invalid"
        ) from None


def _cloudwatch_event(entry: NodeLogEntryIn) -> dict[str, object]:
    """One journal entry as a CloudWatch event: a JSON message, so Logs Insights parses its fields."""
    return {
        "timestamp": int(as_utc(entry.ts).timestamp() * 1000),
        "message": json.dumps(
            {"unit": entry.unit, "priority": entry.priority, "message": entry.message},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }


def _record_log_findings(session: Session, node_id: UUID, entries: list[NodeLogEntryIn]) -> None:
    """Fold the batch's daemon-trouble lines into the node's `log_findings` (`app/node_log_findings.py`),
    committed on its own and before the CloudWatch step, so a CP with no log group still records them. A
    failure here is logged, never the shipment's."""
    try:
        row = session.scalar(select(Node).where(Node.id == node_id))
        if row is not None and node_log_findings.record(row, entries):
            session.commit()
    except Exception:
        session.rollback()
        log.exception("node-logs: recording log findings for node %s failed", node_id)


@router.post("/node-logs", response_model=NodeLogsOut)
@infra(Tenancy.ORG_SCOPED)
def ship_node_logs(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    body: NodeLogsIn = Depends(_node_logs_body),
    session: Session = Depends(get_session),
) -> NodeLogsOut:
    """Write the CALLING node's journal entries to its stream (the node's id) in `NODE_LOG_GROUP`.

    A node token only: the stream is the credential's node, and an arm that derives no node (CP
    loopback, dev bypass) has no journal of its own to ship. The stream is the id, never the name: a name is
    renameable and unique per org only, so a name-keyed stream let one tenant's node write another host's
    lines. Streams keyed by name before this change are legacy (`routes/nodes/journal.py` still reads them).
    The cursor is returned only once every entry is stored; any AWS failure is a 502 and the node resends
    from its old cursor.
    """
    if body.entries and caller.node_id is not None:
        _record_log_findings(session, caller.node_id, body.entries)
    if not edition_features.enabled("node_log_archive"):
        raise HTTPException(status_code=503, detail=_NODE_LOGS_NOT_CONFIGURED)
    from app.aws import cloudwatch_logs

    group = config.node_log_group()
    if group is None:
        raise HTTPException(status_code=503, detail=_NODE_LOGS_NOT_CONFIGURED)
    if not body.entries:
        return NodeLogsOut(cursor=body.cursor)
    if caller.node_id is None:
        raise HTTPException(status_code=404, detail="this credential has no node")
    stream = str(caller.node_id)
    now = datetime.now(timezone.utc)
    oldest, newest = now - NODE_LOGS_MAX_AGE, now + NODE_LOGS_MAX_FUTURE
    events = [
        _cloudwatch_event(entry) for entry in body.entries
        if oldest < as_utc(entry.ts) < newest
    ]
    if len(events) < len(body.entries):
        log.warning(
            "node-logs: node %s sent %d entries outside CloudWatch's window (14 d back, 2 h ahead); not stored",
            stream, len(body.entries) - len(events),
        )
    if not events:
        return NodeLogsOut(cursor=body.cursor)
    if len(cloudwatch_logs.batches(events)) > NODE_LOGS_MAX_CALLS:
        raise HTTPException(status_code=413, detail="node-logs body spans too much time for one request")
    try:
        cloudwatch_logs.put_log_events(group, stream, events)
    except cloudwatch_logs.CloudWatchLogsError as exc:
        # `str(exc)` names the action, status and AWS error code only — never a message.
        log.warning("node-logs: shipping %d entries for node %s failed: %s", len(events), stream, exc)
        raise HTTPException(status_code=502, detail="CloudWatch Logs did not accept the entries; retry") from None
    return NodeLogsOut(cursor=body.cursor)


# ── Node model catalogs, for tier resolution on providers with no moving alias ─────────────────────


# Bounds on one node-reported catalog: it is node-written data that reaches every later dispatch's argv.
_MAX_CATALOG_MODELS = 200


class CatalogModelIn(BaseModel):
    slug: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
    visibility: str = Field(max_length=32)
    priority: int


class NodeModelCatalogsIn(BaseModel):
    # provider value -> that provider CLI's live catalog on this node (`apps/jaahd/provider_models.py`).
    catalogs: dict[str, list[CatalogModelIn]]
    # Only for a caller with no derived identity (CP loopback, dev bypass); a node's own token decides.
    node_id: Optional[str] = None

    @field_validator("catalogs")
    @classmethod
    def _known_catalog_providers(
        cls, value: dict[str, list[CatalogModelIn]]
    ) -> dict[str, list[CatalogModelIn]]:
        # Only a provider that resolves tiers from a catalog may be reported (`tier_aliases == ()`).
        allowed = {p.value for p in agent_providers.PROVIDERS if not p.tier_aliases}
        for provider, models in value.items():
            if provider not in allowed:
                raise ValueError(f"provider {provider!r} does not resolve tiers from a catalog")
            if len(models) > _MAX_CATALOG_MODELS:
                raise ValueError(f"{provider} catalog exceeds {_MAX_CATALOG_MODELS} models")
        return value


@router.post("/nodes/model-catalogs", status_code=204)
@infra(Tenancy.ORG_SCOPED)
def set_node_model_catalogs(
    body: NodeModelCatalogsIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Merge the CALLING node's reported catalogs into `nodes.model_catalogs`, per provider, each stamped
    with the CP's clock. A provider the body omits keeps its last report (absence asserts nothing).

    The node is the credential's, never the body's (`caller_node_id`): a node may only report itself, and
    dispatch reads only the dispatching node's own report (`dispatch_compose._node_model_catalog`)."""
    node_id = caller_node_id(session, caller, body.node_id or "")
    current = session.scalar(select(Node.model_catalogs).where(Node.id == node_id).with_for_update())
    merged = dict(current) if isinstance(current, dict) else {}
    observed_at = datetime.now(timezone.utc).isoformat()
    for provider, models in body.catalogs.items():
        merged[provider] = {
            "observed_at": observed_at,
            "models": [model.model_dump() for model in models],
        }
    session.execute(update(Node).where(Node.id == node_id).values(model_catalogs=merged))
    session.commit()
    return Response(status_code=204)


# ── Node liveness, for the off-host sweep ─────────────────────────────────────
# Reachable from any host, so the sweep reads the live DB rather than a host-local copy.

# `node_id` on this wire is `nodes.name` and must stay one: `node_liveness.py` renders it into
# operator-runnable commands. Names repeat across orgs, so `node_uuid` (`n.id`) is the key anything matches
# on. `worktrees` counts join `n.id`; `ORDER BY t.node_id` is alphabetical.
NODES_LIVENESS_SQL = """
SELECT coalesce(json_agg(t ORDER BY t.node_id)::text, '[]') FROM (
  SELECT n.name AS node_id,
         n.id::text AS node_uuid,
         n.enabled,
         extract(epoch from (now() - n.last_seen))::float8            AS last_seen_age,
         extract(epoch from (now() - n.observed_at))::float8           AS observed_at_age,
         extract(epoch from (now() - n.worktrees_updated_at))::float8  AS worktrees_updated_age,
         n.last_seen::text                                            AS last_seen,
         n.observed_at::text                                          AS observed_at,
         n.config_snapshot->>'version'                                AS generation,
         extract(epoch from (now() - n.config_reported_at))::float8   AS generation_reported_age,
         (SELECT count(*) FROM worktrees w
           WHERE w.node_id = n.id AND w.number > 0)::int               AS slots,
         (SELECT count(*) FROM worktrees w WHERE w.node_id = n.id)::int AS worktree_rows,
         -- The config PAIR the CONFIG-DRIFT verdict compares: the roster's declaration of what this
         -- node's knobs should be, and what the node last reported. Shipped as whole objects and
         -- compared in Python (`node_liveness.config_drift`) rather than diffed in SQL — the comparison
         -- carries a normalization rule (`"ON"` == `on`), and a rule spelled in a query string is one
         -- no hermetic test can reach. `config_snapshot` is passed RAW, never coalesced to `{}`: NULL
         -- means "this node has never reported" and must stay distinguishable from an empty report, or
         -- `config_drift`'s positive-proof posture inverts and every declared node with an old daemon
         -- is accused of drift it was never shown to have.
         n.config_desired                                               AS config_desired,
         n.config_snapshot                                              AS config_reported
  FROM nodes n
) t
"""


class NodeLivenessRow(BaseModel):
    """One `nodes` row, ages computed by this database's `now()`, never the caller's clock. Shape is pinned
    to `apps/api/app/node_liveness.py::NodeRow`/`parse_rows`, its one consumer.
    """
    # `nodes.name` (0106) under its wire spelling — the consumer renders it into SSH/SSM/PUT command text.
    node_id: str
    # `nodes.id` — the identity; `node_id` above is a display name that repeats across orgs.
    node_uuid: str
    enabled: bool
    last_seen_age: Optional[float] = None
    observed_at_age: Optional[float] = None
    worktrees_updated_age: Optional[float] = None
    last_seen: Optional[str] = None
    observed_at: Optional[str] = None
    generation: Optional[str] = None
    generation_reported_age: Optional[float] = None
    slots: int
    worktree_rows: int
    # Config-drift pair: `config_desired` defaults to `{}`; `config_reported` stays None when never reported
    # (see the SQL).
    config_desired: dict[str, Any] = {}
    config_reported: Optional[dict[str, Any]] = None


@router.get("/nodes/liveness", response_model=list[NodeLivenessRow])
@infra(Tenancy.OPERATOR)
def node_liveness(
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> list[NodeLivenessRow]:
    """The fleet-wide liveness roster `apps/api/app/node_liveness.py` asserts against. Read-only: observing
    must not change fleet state.
    """
    raw = session.execute(text(NODES_LIVENESS_SQL)).scalar()
    return [NodeLivenessRow(**row) for row in json.loads(raw or "[]")]


# ── a node asking who it is: uuid → its own human handle ──────────────────────
# A daemon holds only `NODE_ID`; its human/tailnet name (`nodes.name`) is in a table it cannot
# read, so it asks here.


class NodeIdentityOut(RefOut):
    """The asking node's own two identifiers. Consumed by `apps/jaahd`, which writes `name` into
    `/etc/jaah/node.env` as `NODE_NAME` and names its tmux session after it, so the shape is a pinned
    cross-agent contract. `id` is echoed back as a string so the node can confirm which row answered."""


@router.get("/nodes/identity", response_model=NodeIdentityOut)
@infra(Tenancy.ORG_SCOPED)
def node_self_identity(
    node_id: str = Query(..., min_length=1,
                         description="the asking node's own uuid, as `/etc/jaah/node.env` holds it"),
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> NodeIdentityOut:
    """The human handle for the uuid a node already holds. Read-only.

    Uuid-only via `node_identity.wire_node_id_or_404`: a name 404s, since a daemon holding one is
    misprovisioned.
    """
    resolved = wire_node_id_or_404(session, node_id)
    name = names_by_id(session, [resolved]).get(resolved)
    if name is None:
        # An unnamed row is as unresolved as an unknown uuid: a blank handle is useless.
        raise HTTPException(status_code=404, detail=f"node {node_id!r} resolves to a row with no name")
    return NodeIdentityOut(id=str(resolved), name=name)


class TokenRotateOut(BaseModel):
    """The node's next credential, in plaintext, exactly once. Consumed by `apps/jaahd/jaah_cli.py::cmd_auth_rotate`."""

    token: str


def _rotating_node(session: Session, caller: node_credentials.AgentCaller, presented: str) -> tuple[Node, str]:
    """The caller's own row, locked, and which of its credentials it presented (`node_credentials.presented_as`).

    Only a node's own credential rotates: the CP's loopback and the dev bypass derive no node, so 403. The token is
    re-proved against the LOCKED row: `_check_token` resolved it a statement earlier, and a concurrent rotate,
    confirm or console re-issue may have moved it since."""
    if caller.node_id is None:
        raise HTTPException(status_code=403, detail="only a node's own credential can rotate it")
    node = session.scalar(select(Node).where(Node.id == caller.node_id).with_for_update())
    presented_as = node_credentials.presented_as(node, presented) if node is not None else None
    if presented_as is None:
        raise HTTPException(status_code=401, detail="invalid agent token")
    return node, presented_as


@router.post("/token/rotate", response_model=TokenRotateOut)
@infra(Tenancy.ORG_SCOPED)
def rotate_node_token(
    response: Response,
    x_agent_token: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> TokenRotateOut:
    """Mint the calling node's next credential as pending (`node_credentials.rotate`); the current one keeps
    working until `/token/rotate/confirm`. Presented the pending token instead, 409: that node already holds its
    next credential and must confirm it, and minting another would orphan the one it wrote."""
    node, presented_as = _rotating_node(session, caller, x_agent_token)
    if presented_as == node_credentials.ROTATION_PRESENTED_PENDING:
        raise HTTPException(
            status_code=409,
            detail="this is the rotated credential, not yet confirmed - confirm it (POST /api/agent/token/rotate/confirm)",
        )
    token = node_credentials.rotate(node)
    session.commit()
    log.info("node-auth: node %s began a credential rotation", node.id)
    response.headers["Cache-Control"] = "no-store"
    return TokenRotateOut(token=token)


@router.post("/token/rotate/confirm", status_code=204)
@infra(Tenancy.ORG_SCOPED)
def confirm_node_token_rotation(
    x_agent_token: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Response:
    """Presented the pending token: promote it, and the previous one stops resolving. Presented the current one
    with nothing pending: 204, so a confirm whose answer was lost can be repeated. Presented the current one while
    a rotation is pending: 409 — only the new token proves the node holds it."""
    node, presented_as = _rotating_node(session, caller, x_agent_token)
    if presented_as == node_credentials.ROTATION_PRESENTED_PENDING:
        node_credentials.confirm_rotation(node)
        emit_console_event(session, entity="node", id=str(node.id), action="upserted")
        session.commit()
        log.info("node-auth: node %s confirmed its rotated credential; the previous one is revoked", node.id)
    elif node.pending_node_token_hash is not None:
        raise HTTPException(
            status_code=409,
            detail="confirm with the rotated credential (the token /api/agent/token/rotate returned), not the current one",
        )
    return Response(status_code=204)


@router.post("/disk-reclaims", response_model=node_disk_events.DiskReclaimOut)
@infra(Tenancy.ORG_SCOPED)
def post_disk_reclaim(
    body: node_disk_events.DiskReclaimIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> node_disk_events.DiskReclaimOut:
    """One reclaim the calling node ran, stored as a `reclaim` row of its disk history
    (`app/node_disk_events.py`). The node is the credential's own, never a claim; a replay of a `reclaim_id`
    is a 200 no-op, so the node's spool may resend freely."""
    if caller.node_id is None:
        raise HTTPException(
            status_code=403,
            detail="disk reclaim reports require a per-node credential; this caller presented a token that names no node",
        )
    stored = node_disk_events.record_reclaim(session, caller.node_id, body)
    session.commit()
    return node_disk_events.DiskReclaimOut(stored=stored)
