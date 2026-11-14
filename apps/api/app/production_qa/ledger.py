"""The ledger applier (`apply_edges`, architecture C10): `production_qa_edges` into the outcome ledger, through its
only writer (`outcome_ledger.writer`) — the one place Production QA touches `task_outcomes` (risk 13).

`applied_at` means "this fact is in the ledger now". Each pass takes, in id order:
* an unrevoked edge not applied → the writer's `record_*`; stamped `applied_at` unless the writer deferred it (the
  task's newest dispatch has not merged yet) or failed (it swallows and logs; the next pass retries).
* a revoked edge that was applied → the writer's dismissal (`withdraw_*`), then `applied_at` cleared: nothing of it
  is in the ledger any more. A revoked edge never applied needs nothing.
So a crash between the writer and the stamp re-applies the edge, and every writer call is repeat-safe; a pass with
nothing to do changes nothing. An edge un-revoked after its dismissal (`applied_at` NULL again) is applied anew.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.production_qa import Incident, IncidentSuspect, ProductionQaEdge
from app.outcome_ledger import writer
from app.production_qa import cron

log = logging.getLogger("cockpit-production-qa")

# Edges per commit, and the time a pass may spend (it shares `cron.TIMEOUT_SECONDS` with every other job).
BATCH = 200
PASS_SECONDS = 20.0

# Where the last pass stopped: the next one resumes there and wraps once, so edges that keep waiting (a dispatch not
# merged yet, a failing write) never starve newer ones. Process-local; a restart starts again at the lowest id.
_cursor = 0

_DUE = or_(
    and_(ProductionQaEdge.applied_at.is_(None), ProductionQaEdge.revoked_at.is_(None)),
    and_(ProductionQaEdge.applied_at.is_not(None), ProductionQaEdge.revoked_at.is_not(None)),
)


def _confirmed(session: Session, edge: ProductionQaEdge) -> bool:
    """Whether an operator confirmed one of the task's suspects on the edge's incident."""
    return bool(session.scalar(select(func.count()).select_from(IncidentSuspect).where(
        IncidentSuspect.incident_id == edge.incident_id, IncidentSuspect.task_id == edge.task_id,
        IncidentSuspect.state == "confirmed",
    )))


def _still_deployed_at(session: Session, edge: ProductionQaEdge) -> datetime | None:
    """The earliest other `deployed` fact of the task still in the ledger."""
    return session.scalar(select(func.min(ProductionQaEdge.occurred_at)).where(
        ProductionQaEdge.task_id == edge.task_id, ProductionQaEdge.kind == "deployed", ProductionQaEdge.id != edge.id,
        ProductionQaEdge.revoked_at.is_(None), ProductionQaEdge.applied_at.is_not(None),
    ))


def _record(session: Session, edge: ProductionQaEdge) -> str | None:
    if edge.kind == "deployed":
        return writer.record_deployed(session, edge.task_id, edge.occurred_at)
    if edge.kind == "incident_linked":
        title = session.scalar(select(Incident.title).where(Incident.id == edge.incident_id))
        return writer.record_incident_link(session, edge.task_id, edge.incident_id, title=title,
                                           confirmed=_confirmed(session, edge))
    return writer.record_fix(session, edge.task_id, edge.incident_id)


def _withdraw(session: Session, edge: ProductionQaEdge) -> str | None:
    if edge.kind == "deployed":
        return writer.withdraw_deployed(session, edge.task_id, _still_deployed_at(session, edge))
    if edge.kind == "incident_linked":
        return writer.withdraw_incident_link(session, edge.task_id, edge.incident_id)
    return writer.FACT_APPLIED  # `resolved_by_fix` wrote nothing on the row.


def apply_one(session: Session, edge: ProductionQaEdge, now: datetime) -> bool:
    """Apply or withdraw one due edge; returns whether its `applied_at` changed."""
    if edge.revoked_at is None:
        if _record(session, edge) != writer.FACT_APPLIED:
            return False
        edge.applied_at = now
        return True
    if _withdraw(session, edge) != writer.FACT_APPLIED:
        return False
    edge.applied_at = None
    return True


def apply_edges(session: Session, now: datetime) -> None:
    """The `apply_edges` job: due edges in id order from where the last pass stopped, wrapping once to the lowest
    id, a commit per batch, until all were seen or `PASS_SECONDS` ran out; rows another pass holds are skipped.
    Each write lands in the edge's own org: the writer stamps the outcome row's `org_id`, never a carried one."""
    global _cursor
    deadline = time.monotonic() + PASS_SECONDS
    start = after = _cursor
    wrapped = start == 0
    while time.monotonic() < deadline:
        query = select(ProductionQaEdge).where(_DUE, ProductionQaEdge.id > after)
        if wrapped and start:
            query = query.where(ProductionQaEdge.id <= start)
        edges = list(session.scalars(
            query.order_by(ProductionQaEdge.id).limit(BATCH).with_for_update(skip_locked=True)
        ))
        if not edges:
            if wrapped:
                _cursor = 0
                return
            wrapped, after = True, 0
            continue
        for edge in edges:
            apply_one(session, edge, now)
        session.commit()
        after = _cursor = edges[-1].id


cron.register("apply_edges", apply_edges)
