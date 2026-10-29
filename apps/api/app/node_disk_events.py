"""The node disk gate and the node's disk history.

THE GATE. `nodes.disk_gate` is `open` or `paused`. Decided on the observation path
(`fleet_observe.apply_observation` → `apply_reading`), from the reading the pass just carried, judged by
`node_host_metrics.classify` — the thresholds are that module's (`WARN_PCT`, `CRITICAL_PCT`), never re-spelled:

    reading      open                     paused
    ok           stay open                RESUME (runners + CI, unattended)
    warn         stay open (node reclaims) stay paused (hysteresis)
    critical     PAUSE + alert            stay paused
    unknown      stay open (fail open)    stay paused (never resumes on an unknown reading)

A pass with no disk reading (an older daemon, an implausible block) decides nothing. Each transition writes a
`pause`/`resume` row here, and — after the observation commits (`after_commit`) — pokes the node's gha pool so
its CI runners stop or restart now rather than on the node's next 60s pass; a pause also raises one operator
alert through the existing channel (`alert_record.record_alert`).

What the gate stops: runner launches (`app/launch_disk.py`, the one home of "may a runner launch here,
disk-wise") and CI jobs (`served_gha_state`: the agent desired endpoint serves a running pool as `paused`, so
the existing drain stops only idle runners; a running job finishes). The operator's own `desired_state` is
never written: an operator pause outlives a disk recovery, and the gate resumes only what it paused.

THE HISTORY. `node_disk_events` holds each `pause`/`resume` and each `reclaim` the node reports
(`record_reclaim`, `POST /api/agent/disk-reclaims`), kept `RETENTION_DAYS` and pruned on ingest; the node page
reads it through `events_for`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Iterable, Literal, Optional, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, create_model, model_validator

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import edition_features, node_host_metrics
from app.entity_shape import SYSTEM_ACTOR_EMAIL, StrictModel
from app.models.nodes import Node, NodeDiskEvent

log = logging.getLogger("app.node_disk_events")

GATE_OPEN = "open"
GATE_PAUSED = "paused"

KIND_RECLAIM = "reclaim"
KIND_PAUSE = "pause"
KIND_RESUME = "resume"

#: What started a reclaim (`apps/jaahd`'s reclaimer reports one).
Trigger = Literal["scheduled", "warn", "converge"]
TRIGGERS: tuple[str, ...] = get_args(Trigger)

#: The CLOSED set of reclaim categories a report may name; an unknown key is a 422. Each producer sends its own.
FREED_KEYS = (
    "ci_images", "build_cache", "stopped_containers", "orphan_volumes",
    "runner_versions", "runner_temp", "plugin_dedup", "orphan_users", "stale_tmp",
)

#: The reclaim-report keys kept in `detail` (everything after `freed_bytes` but the errors), as reported.
DETAIL_KEYS = ("used_pct_before", "engines", "removed_users", "remaining_bytes")

RETENTION_DAYS = 30

#: The alert kind a pause raises (`alert_relay.ALERT_KINDS`).
ALERT_KIND = "node-disk-paused"

#: Who paused a gha pool, as the console and the node are told.
PAUSED_BY_OPERATOR = "operator"
PAUSED_BY_DISK = "disk"


# ── The rule (pure) ─────────────────────────────────────────────────────────────────────────────────


def next_gate(current: str, used_pct: Optional[float]) -> Optional[str]:
    """The state this reading moves the gate to, or None for no change. Pure; the table is the module docstring."""
    level = node_host_metrics.classify(used_pct)
    if current == GATE_OPEN and level == node_host_metrics.LEVEL_CRITICAL:
        return GATE_PAUSED
    if current == GATE_PAUSED and level == node_host_metrics.LEVEL_OK:
        return GATE_OPEN
    return None


def reading_pct(block: Any) -> Optional[float]:
    """The used percentage an observation's `host_metrics` block carries, or None when it carries no readable
    disk (absent, a non-positive total, a negative free)."""
    if block is None:
        return None
    free, total = getattr(block, "disk_free_kb", None), getattr(block, "disk_total_kb", None)
    if free is None or total is None or total <= 0 or free < 0 or free > total:
        return None
    return node_host_metrics.disk_used_pct(free, total)


def served_gha_state(desired_state: str, disk_gate: Optional[str]) -> tuple[str, Optional[str]]:
    """`(state the node is served, paused_by)` for a gha pool. The operator's pause wins and is named theirs;
    a running pool on a disk-paused node is served `paused` by the disk; `absent` is never overridden."""
    if desired_state == "paused":
        return "paused", PAUSED_BY_OPERATOR
    if desired_state == "running" and disk_gate == GATE_PAUSED:
        return "paused", PAUSED_BY_DISK
    return desired_state, None


def pause_reason(node_name: str, used_pct: Optional[float]) -> str:
    """The operator alert's sentence; `used_pct` None leaves the figure out (`on_foreign_host`)."""
    usage = f"disk {used_pct:.0f}% used" if used_pct is not None else "disk full"
    return (
        f"Node {node_name} {usage}: no new runner or CI job starts on it until usage drops "
        f"under {node_host_metrics.WARN_PCT:g}%."
    )


def on_foreign_host(session: Session, node_uuid: UUID) -> bool:
    """Is this a node co-located on ANOTHER org's host? Its disk reading is then the host's filesystem, a
    figure its own org is never told (`routes/nodes/node_wire.py::_narrow_for_tenant`), so every surface that reaches
    that org — the wait reason, the alert, the history — leaves the percentage out."""
    if not edition_features.enabled("multi_node_hosts"):
        return False  # The community edition has no Multi-Node Hosts.
    from app import vnodes

    fact = vnodes.hosting_facts(session, [node_uuid]).get(node_uuid)
    if fact is None or fact.is_host or fact.host_org_id is None:
        return False
    return fact.host_org_id != session.scalar(select(Node.org_id).where(Node.id == node_uuid))


# ── The observation path ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateTransition:
    """One gate decision, carried from the observation's transaction to its post-commit effects."""

    node_id: UUID
    node_name: str
    to_state: str
    used_pct: float
    event_id: int


def _event_insert(node_uuid: UUID, **values: Any):
    """A Core insert carrying the node's org explicitly (the inherit trigger is the backstop)."""
    return pg_insert(NodeDiskEvent).values(
        node_id=node_uuid, org_id=select(Node.org_id).where(Node.id == node_uuid).scalar_subquery(), **values,
    )


def prune_statement(node_uuid: UUID):
    """Drop this node's events older than `RETENTION_DAYS`. Rides each ingest, like `node_host_metrics`'s."""
    return delete(NodeDiskEvent).where(
        NodeDiskEvent.node_id == node_uuid,
        NodeDiskEvent.occurred_at < func.now() - func.make_interval(0, 0, 0, RETENTION_DAYS),
    )


def apply_reading(
    session: Session, node_uuid: UUID, node_name: str, block: Any, *, disk_gate: Optional[str]
) -> Optional[GateTransition]:
    """Decide the gate from one observation's `host_metrics` block, in the caller's transaction (the `nodes`
    row is already locked by the pass's stamp). Returns the transition for `after_commit`, or None.

    `disk_gate` is the row's current gate as the caller read it under that lock (the stamp's RETURNING), so
    it is the value a read here would see; None (no row) decides nothing."""
    pct = reading_pct(block)
    if pct is None:
        return None
    current = disk_gate
    if current is None:
        return None
    target = next_gate(current, pct)
    if target is None:
        return None
    session.execute(
        update(Node).where(Node.id == node_uuid)
        .values(disk_gate=target, disk_gate_changed_at=func.now())
        .execution_options(synchronize_session=False)
    )
    event_id = session.execute(_event_insert(
        node_uuid, kind=KIND_PAUSE if target == GATE_PAUSED else KIND_RESUME, used_pct=round(pct, 1),
    ).returning(NodeDiskEvent.id)).scalar_one()
    session.execute(prune_statement(node_uuid))
    log.info("disk gate: node %s %s at %.1f%% disk used", node_name, target, pct)
    return GateTransition(node_id=node_uuid, node_name=node_name, to_state=target, used_pct=pct, event_id=event_id)


def after_commit(transition: Optional[GateTransition]) -> None:
    """The transition's effects outside the observation's transaction: the operator alert on a pause, and
    the gha poke both ways. Best effort and never raises: the gate is committed, the node's own pass applies
    the served state within a minute anyway."""
    if transition is None:
        return
    from app import alert_record
    from app.db import SessionLocal
    from app.models.node_groups import GhaRunnerPool

    session = SessionLocal()
    try:
        project_id = session.scalar(
            select(GhaRunnerPool.project_id).where(GhaRunnerPool.node_id == transition.node_id)
        )
        figure = None if on_foreign_host(session, transition.node_id) else transition.used_pct
        session.rollback()  # `record_alert` needs a session with no open work
        if transition.to_state == GATE_PAUSED:
            # The node and the pause ride `issue`: `uq_alerts_dedup` covers neither `node_id` nor a pause, so
            # two nodes pausing — or one node pausing twice — in one window would otherwise collapse into one.
            alert_record.record_alert(
                session, kind=ALERT_KIND, node_id=str(transition.node_id),
                issue=f"disk:{transition.node_id}:{transition.event_id}",
                reason=pause_reason(transition.node_name, figure), tell_org_admins=True,
            )
        if project_id is not None:
            from app.durable import gha_pool

            gha_pool.enqueue_apply(project_id, transition.node_id, transition.node_name, SYSTEM_ACTOR_EMAIL)
    except Exception as exc:  # noqa: BLE001 — an effect must never break the pass it follows
        log.warning("disk gate: post-commit effects for node %s failed: %s", transition.node_name, exc)
    finally:
        session.close()


# ── Reclaim reports ─────────────────────────────────────────────────────────────────────────────────

_Bytes = Annotated[int, Field(ge=0, le=2**63 - 1)]
_Pct = Annotated[float, Field(ge=0.0, le=100.0)]
_Text = Annotated[str, Field(max_length=256, pattern=r"^[^\x00]*$")]
_Name = Annotated[str, Field(max_length=64, pattern=r"^[^\x00]*$")]
_LIST_MAX = 64

#: `freed_bytes`, its keys derived from `FREED_KEYS` so the closed set has one spelling; an unknown key is a 422.
FreedBytesIn = create_model(
    "FreedBytesIn", __base__=StrictModel, **{key: (Optional[_Bytes], None) for key in FREED_KEYS},
)


class ReclaimEngineIn(StrictModel):
    """One runner user's container engine as the reclaim left it."""

    user: _Name
    uid: Optional[int] = None
    bytes_before: Optional[_Bytes] = None
    bytes_after: Optional[_Bytes] = None
    volumes_bytes_after: Optional[_Bytes] = None
    skipped: Optional[Literal["busy"]] = None
    error: Optional[_Text] = None


class DiskReclaimIn(StrictModel):
    """One reclaim the node ran (`POST /api/agent/disk-reclaims`). `reclaim_id` is the idempotency key; every
    key after `freed_bytes` is optional, and a producer sends only its own `freed_bytes` keys."""

    reclaim_id: UUID
    started_at: AwareDatetime
    finished_at: AwareDatetime
    trigger: Trigger
    used_pct_before: _Pct
    used_pct_after: _Pct
    freed_bytes: FreedBytesIn
    engines: Optional[list[ReclaimEngineIn]] = Field(default=None, max_length=_LIST_MAX)
    removed_users: Optional[list[_Name]] = Field(default=None, max_length=_LIST_MAX)
    remaining_bytes: Optional[dict[_Name, _Bytes]] = Field(default=None, max_length=16)
    errors: Optional[list[_Text]] = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _finished_after_started(self) -> "DiskReclaimIn":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


class DiskReclaimOut(BaseModel):
    """`stored` false is a replay of a `reclaim_id` already held: a no-op, still a 200."""

    stored: bool


def record_reclaim(session: Session, node_uuid: UUID, report: Any) -> bool:
    """Store one reclaim report as a `reclaim` row, in the caller's transaction. True when stored, False on a
    replay of a `reclaim_id` already held (a no-op). `report` is the route's validated body."""
    freed = report.freed_bytes.model_dump(exclude_none=True)
    detail = report.model_dump(mode="json", include=set(DETAIL_KEYS), exclude_none=True)
    if report.errors:
        detail["errors"] = list(report.errors)
    stored = session.execute(
        _event_insert(
            # A node clock ahead of ours must not pin its row to the top of the newest-first history.
            node_uuid, kind=KIND_RECLAIM, occurred_at=func.least(report.finished_at, func.now()),
            trigger=report.trigger,
            used_pct=report.used_pct_after, freed=freed, detail=detail, reclaim_id=report.reclaim_id,
        )
        .on_conflict_do_nothing(constraint="uq_node_disk_events_reclaim_id")
        .returning(NodeDiskEvent.id)
    ).first()
    session.execute(prune_statement(node_uuid))
    return stored is not None


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────────


def events_for(
    session: Session, node_uuid: UUID, *, host_uuid: Optional[UUID] = None, limit: int = 50,
) -> list[NodeDiskEvent]:
    """This node's events, newest first, plus its host's `reclaim` rows when `host_uuid` names one (a
    co-located node shares its host's filesystem, so the host's reclaims are this node's too)."""
    clause = NodeDiskEvent.node_id == node_uuid
    if host_uuid is not None:
        clause = clause | ((NodeDiskEvent.node_id == host_uuid) & (NodeDiskEvent.kind == KIND_RECLAIM))
    return list(session.scalars(
        select(NodeDiskEvent).where(clause)
        .order_by(NodeDiskEvent.occurred_at.desc(), NodeDiskEvent.id.desc()).limit(limit)
    ))


def gates_by_id(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, tuple[str, datetime]]:
    """`{node id: (disk_gate, disk_gate_changed_at)}` in one read."""
    wanted = sorted({node_uuid for node_uuid in node_uuids if node_uuid is not None})
    if not wanted:
        return {}
    rows = session.execute(
        select(Node.id, Node.disk_gate, Node.disk_gate_changed_at).where(Node.id.in_(wanted))
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def fresh_pct(reading: Optional[node_host_metrics.LatestReading]) -> Optional[float]:
    """The reading's used percentage while it is fresh, else None."""
    if reading is None or reading.stale:
        return None
    return reading.disk_pct
