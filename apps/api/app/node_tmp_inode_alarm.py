"""The /tmp inode alarm: one operator alert when a node's /tmp inode use crosses `ALARM_PCT`.

A tmpfs /tmp can run out of inodes with gigabytes free, and then every `mktemp` on the node fails.
The Health tab's `tmp_inodes_high` finding (`app/node_log_findings.py`) and the meter's flag show it only to
someone looking; this raises it through the disk alarm's channel (`alert_record.record_alert`).

Decided on the observation path (`fleet_observe.apply_observation`), from the node's previous reading and the
one the pass carries: the alert fires on the UPWARD crossing only (previous at or under `ALARM_PCT`, or none,
and the new one above), so a node that stays full raises one alert, not one a minute. It then re-arms only
once the node has read at or under `node_host_metrics.TMP_INODE_WARN_PCT` since its last alert (`_rearmed`,
off the stored readings, so no state of its own), so /tmp churning around 90% is one alert, not one per dedup
window. Raised after the observation commits (`after_commit`), so a deadlock retry's rolled-back reading never
alerts. Only a node with its own /tmp alerts: a co-located node's /tmp is its host's filesystem, so the host
raises the one alert for it — and a co-located node on another org's host must never be told that figure
(`routes/nodes/node_wire.py::_own_disk_only`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app import edition_features, node_host_metrics
from app.models.nodes import NodeHostMetric
from app.models.notifications import Alert

log = logging.getLogger("app.node_tmp_inode_alarm")

#: The alert kind (`alert_relay.ALERT_KINDS`).
ALERT_KIND = "node-tmp-inodes"

#: Used percentage above which (strictly) the alarm fires: the disk gate's critical line.
ALARM_PCT = node_host_metrics.CRITICAL_PCT


def crossed(previous_pct: Optional[float], pct: Optional[float]) -> bool:
    """Whether `pct` crosses `ALARM_PCT` upward from `previous_pct` (None: no earlier reading). Pure."""
    if pct is None or pct <= ALARM_PCT:
        return False
    return previous_pct is None or previous_pct <= ALARM_PCT


def reason(node_name: str, pct: float) -> str:
    """The operator alert's sentence."""
    return f"Node {node_name} /tmp inodes {pct:.0f}% used: every mktemp on it fails at 100%."


@dataclass(frozen=True)
class Crossing:
    """One upward crossing, carried from the observation's transaction to `after_commit`."""

    node_id: UUID
    node_name: str
    pct: float


def previous_pct(session: Session, node_uuid: UUID) -> Optional[float]:
    """The node's newest stored /tmp inode use, read BEFORE this pass's upsert overwrites it; None when none."""
    row = session.execute(
        select(NodeHostMetric.tmp_inodes_free, NodeHostMetric.tmp_inodes_total)
        .where(NodeHostMetric.node_id == node_uuid)
        .order_by(NodeHostMetric.bucket_at.desc())
        .limit(1)
    ).first()
    return None if row is None else node_host_metrics.tmp_inode_used_pct(row.tmp_inodes_free, row.tmp_inodes_total)


def judge(node_uuid: UUID, node_name: str, previous: Optional[float], block: Any) -> Optional[Crossing]:
    """The crossing this pass's `host_metrics` block makes, or None. Pure."""
    pct = node_host_metrics.tmp_inode_used_pct(
        getattr(block, "tmp_inodes_free", None), getattr(block, "tmp_inodes_total", None)
    )
    if not crossed(previous, pct):
        return None
    return Crossing(node_id=node_uuid, node_name=node_name, pct=pct)


def _rearmed(session: Session, node_uuid: UUID, last_alert_at: datetime) -> bool:
    """Whether the node has read at or under `TMP_INODE_WARN_PCT` since its last alert."""
    total = cast(NodeHostMetric.tmp_inodes_total, Float(precision=53))
    used_pct = 100.0 * (total - NodeHostMetric.tmp_inodes_free) / total
    return session.scalar(
        select(NodeHostMetric.id).where(
            NodeHostMetric.node_id == node_uuid,
            NodeHostMetric.updated_at > last_alert_at,
            NodeHostMetric.tmp_inodes_free.is_not(None),
            NodeHostMetric.tmp_inodes_total > 0,
            used_pct <= node_host_metrics.TMP_INODE_WARN_PCT,
        ).limit(1)
    ) is not None


def after_commit(crossing: Optional[Crossing]) -> None:
    """Raise the operator alert, unless the node is co-located or not re-armed. Best effort and never raises:
    the reading is committed and the Health tab shows it anyway."""
    if crossing is None:
        return
    from app import alert_record
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        colocated = False
        if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
            from app import vnodes

            fact = vnodes.hosting_facts(session, [crossing.node_id]).get(crossing.node_id)
            colocated = fact is not None and not fact.is_host
        prior = session.execute(
            select(func.count(), func.max(Alert.created_at))
            .where(Alert.node_id == crossing.node_id, Alert.kind == ALERT_KIND)
        ).one()
        silent = colocated or (
            prior[1] is not None and not _rearmed(session, crossing.node_id, prior[1])
        )
        session.rollback()  # `record_alert` needs a session with no open work
        if silent:
            return
        # The node and its alert count ride `issue`: `uq_alerts_dedup` covers neither `node_id` nor an episode.
        alert_record.record_alert(
            session, kind=ALERT_KIND, node_id=str(crossing.node_id),
            issue=f"tmp-inodes:{crossing.node_id}:{prior[0]}",
            reason=reason(crossing.node_name, crossing.pct), tell_org_admins=True,
        )
    except Exception as exc:  # noqa: BLE001 — an effect must never break the pass it follows
        log.warning("tmp inode alarm: alert for node %s failed: %s", crossing.node_name, exc)
    finally:
        session.close()
