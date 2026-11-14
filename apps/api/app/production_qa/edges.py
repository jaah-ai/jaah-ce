"""The outcome-ledger facts (`production_qa_edges`): written here, applied to the ledger once it exists."""
from __future__ import annotations


from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.production_qa import Deployment, DeploymentPr, ProductionQaEdge


def on_deployment(session: Session, deployment: Deployment) -> None:
    """A `deployed` edge per PR task of an anchor (shipped to production from a push, range not a rollback); its
    `occurred_at` — the run's finish — starts the task's survival clock. Repeat-safe on the edge's unique key. A task
    no longer in this deployment's PRs (a recompute narrowed the range, or the run stopped being an anchor) has its
    edge revoked, never deleted."""
    tasks: set[int] = set()
    if deployment.is_anchor and deployment.range_status not in (None, "pending", "rollback"):
        tasks = {t for t in session.scalars(
            select(DeploymentPr.task_id).where(DeploymentPr.deployment_id == deployment.id,
                                               DeploymentPr.task_id.is_not(None))
        )}
    at = deployment.finished_at or deployment.last_received_at or utcnow()
    for task_id in sorted(tasks):
        session.execute(
            pg_insert(ProductionQaEdge)
            .values(org_id=deployment.org_id, task_id=task_id, kind="deployed", deployment_id=deployment.id,
                    occurred_at=at)
            .on_conflict_do_nothing()
        )
    session.execute(
        update(ProductionQaEdge)
        .where(ProductionQaEdge.deployment_id == deployment.id, ProductionQaEdge.kind == "deployed",
               ProductionQaEdge.revoked_at.is_(None), ProductionQaEdge.task_id.not_in(tasks or {0}))
        .values(revoked_at=utcnow())
    )
    # The clock starts at the run's finish: an edge written when production reported success, before the run
    # concluded, moves to the finish once it is known (until the ledger has applied it).
    if deployment.finished_at is not None:
        session.execute(
            update(ProductionQaEdge)
            .where(ProductionQaEdge.deployment_id == deployment.id, ProductionQaEdge.kind == "deployed",
                   ProductionQaEdge.applied_at.is_(None), ProductionQaEdge.occurred_at != deployment.finished_at)
            .values(occurred_at=deployment.finished_at)
        )
    # A task whose edge was revoked and that ships here again gets it back.
    if tasks:
        session.execute(
            update(ProductionQaEdge)
            .where(ProductionQaEdge.deployment_id == deployment.id, ProductionQaEdge.kind == "deployed",
                   ProductionQaEdge.revoked_at.is_not(None), ProductionQaEdge.task_id.in_(tasks))
            .values(revoked_at=None)
        )
    session.flush()
