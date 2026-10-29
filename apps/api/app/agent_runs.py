"""The SDK agent a task's run uses, read off the task's own claim stamps (`run_agent_id`, `run_agent_commit`).

One reader for every surface that names the agent of a run: the SDK's `agent-context` route and the
notification legs (`app/task_notify.py`), which head a question, done or failed message with the agent's
display name. The stamp must name an agent the task's org may run — its own, or a catalog connector it
enabled — the same rule `routes/agent/task_reads.py::agent_task_bundle` serves code under, so a stamp writer that
missed that check never turns a name into a read of another org's row.

Read on a fleet session of its own: a catalog connector is a row of org zero, which a tenant's bound
session cannot see (`routes/agents.py::_catalog_rows`). Read-only; nothing is returned but plain values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select

from app.models.agent_sessions import Agent, AgentEnablement


@dataclass(frozen=True)
class RunAgent:
    id: UUID
    display_name: str
    version: str
    # The commit THIS run uses (the task's stamp), never the agent's newer `resolved_commit`.
    commit: Optional[str]


def run_agent(org_id: UUID, run_agent_id: Optional[UUID], run_agent_commit: Optional[str]) -> Optional[RunAgent]:
    """The agent a run names, or None when the task runs a built-in (no stamp) or the stamp names an agent
    its org may not run."""
    if run_agent_id is None:
        return None
    from app.db import SessionLocal
    from app.tenancy import seam

    enabled = select(AgentEnablement.agent_id).where(
        AgentEnablement.agent_id == run_agent_id, AgentEnablement.org_id == org_id
    )
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        row = fleet.execute(
            select(Agent.id, Agent.display_name, Agent.version).where(
                Agent.id == run_agent_id,
                or_(Agent.org_id == org_id, Agent.id.in_(enabled)),
            )
        ).first()
    if row is None:
        return None
    return RunAgent(id=row.id, display_name=row.display_name, version=row.version, commit=run_agent_commit)


def run_agent_name(task) -> Optional[str]:
    """The display name of the agent `task`'s run uses, or None — the notification legs' one question."""
    agent = run_agent(task.org_id, task.run_agent_id, task.run_agent_commit)
    return agent.display_name if agent is not None else None
