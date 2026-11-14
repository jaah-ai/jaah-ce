"""The node gate driver's calls: one gate's state in a run (`pipeline.gate_event`), the two gates the control
plane judges, open alerts (`pipeline.alerts`) and ticket approval (`pipeline.ticket`, `app/pipeline_gates/remote.py`),
and the release executor's deploy or verify outcome on an arrival (`pipeline.arrival_event`,
`app/releases/arrivals.py`). The
caller is the merge lane's or the release executor's toolbox on a node, through the daemon's TASK-scoped verb, never
the model. Only the task holding the run's work may report on it (`app/pipeline_gates/results.py`); everything else
is the same 404. A canceled run answers 409: the driver stops.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.tasks import Task
from app.pipeline_gates import remote, results
from app.releases import arrivals
from app.routes.agent.toolbox import ToolboxCall, toolbox_router

_NOT_FOUND = "no gate run for this task"
_NO_ARRIVAL = "no arrival for this task"


def _event(session: Session, task_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(_NOT_FOUND)
    return results.apply_event(session, task, body)


def _held(handler: Any) -> Any:
    def call(session: Session, task_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        task = session.get(Task, task_id)
        if task is None:
            raise LookupError(_NOT_FOUND)
        return handler(session, task, body)
    return call


_REFUSALS = ((LookupError, 404, lambda _exc: _NOT_FOUND), (ValueError, 422, None), (KeyError, 422, None))


def _arrival(session: Session, task_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(_NO_ARRIVAL)
    return arrivals.apply_event(session, task, body)


router = toolbox_router(
    tag="agent-pipeline-gates",
    not_found=_NOT_FOUND,
    persona=None,
    calls=(
        ToolboxCall(
            "POST", "/api/agent/pipeline/gate-events", "post_pipeline_gate_event",
            "One gate's state in a run: `{run_id, position, state, attempts, report?, link?}` → `{ok, run_state}`; "
            "404 for a run this task does not hold, 409 for a canceled one, 422 for an unknown state.",
            _event,
            refusals=((LookupError, 404, lambda _exc: _NOT_FOUND), (results.Conflict, 409, None),
                      (ValueError, 422, None), (KeyError, 422, None)),
        ),
        ToolboxCall(
            "POST", "/api/agent/pipeline/alerts", "post_pipeline_alerts",
            "An open-alerts gate's answer: `{run_id, position}` → `{open: [{title, severity, url}], "
            "source_connected}`, read off the stored gate and the incident ledger; 404 for a run this task does not "
            "hold.",
            _held(remote.alerts), refusals=_REFUSALS,
        ),
        ToolboxCall(
            "POST", "/api/agent/pipeline/ticket", "post_pipeline_ticket",
            "A ticket gate's answer: `{run_id, position}` → `{passed, report, link?}`, with the stored gate's "
            "credential, query or the ticket typed when the step was pressed; 404 for a run this task does not hold.",
            _held(remote.ticket), refusals=_REFUSALS,
        ),
        ToolboxCall(
            "POST", "/api/agent/pipeline/arrival-events", "post_pipeline_arrival_event",
            "The release executor's deploy or verify outcome on an arrival: `{arrival_id, state: arrived|"
            "deploy_failed|verified|verify_failed, url?, report?}` → `{ok, state}`; 404 for an arrival this executor "
            "does not hold, 409 for one that moved on (superseded or settled), 422 for an unknown state.",
            _arrival,
            refusals=((LookupError, 404, lambda _exc: _NO_ARRIVAL), (arrivals.Conflict, 409, None),
                      (ValueError, 422, None)),
        ),
    ),
)
