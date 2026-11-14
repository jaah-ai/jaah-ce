"""The deploy probe run's two calls: its plan (`production_qa.probe_plan`) and its results
(`production_qa.probe_result`). The caller is `tools/deploy_probe.py` on a node, through the daemon's TASK-scoped verbs,
never the model (the door and the refusals: `routes/agent/toolbox.py`). It must be an open `deploy_probe` task;
everything else is the same 404, so a runner can neither read nor report another deploy's probes. The rules are
`app/production_qa/probes.py`'s.
"""
from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool

from app.production_qa import events, probes
from app.routes.agent.toolbox import ToolboxCall, toolbox_router


async def _raise_incidents(result: tuple[dict[str, Any], list[events.RaiseIntent]]) -> dict[str, Any]:
    """After the commit and off the loop: each raise writes in a sync session of its own (`events.flush`)."""
    out, intents = result
    await run_in_threadpool(events.flush, intents)
    return out


router = toolbox_router(
    tag="agent-deploy-probe",
    not_found="no deploy probe run for this task",
    persona=probes.PROBE_PERSONA,
    calls=(
        ToolboxCall(
            "GET", "/api/agent/production-qa/probe-plan", "get_deploy_probe_plan",
            "The run's probes (health URLs, smoke commands, limits), or `skipped` with the reason when the gate is off.",
            probes.plan, refusals=((LookupError, 404, None),),
        ),
        ToolboxCall(
            "POST", "/api/agent/production-qa/probe-result", "post_deploy_probe_result",
            "The run's results, once: failures become `health_breach` incidents, passes resolve them. 422 for a shape no "
            "report could mean, 409 for a second report.",
            probes.ingest,
            refusals=((probes.ResultConflict, 409, None), (ValueError, 422, None), (LookupError, 404, None)),
            after=_raise_incidents,
        ),
    ),
)
