"""The outcome scan's two calls: its watch list (`outcome_scan.plan`) and its facts (`outcome_scan.report`). The caller
is `tools/outcome_scan.py` on a node, through the daemon's TASK-scoped verbs, never the model (the door and the
refusals: `routes/agent/toolbox.py`). It must be an open `outcome_scan` task; everything else is the same 404. The
rules are `app/outcome_ledger/scan.py`'s; wire: `apps/api/tests/fixtures/outcome_scan_wire.json`.
"""
from __future__ import annotations

from app.outcome_ledger import scan
from app.routes.agent.toolbox import ToolboxCall, toolbox_router

router = toolbox_router(
    tag="agent-outcome-scan",
    not_found="no outcome scan for this task",
    persona=scan.SCAN_PERSONA,
    calls=(
        ToolboxCall(
            "GET", "/api/agent/outcome-scan/plan", "get_outcome_scan_plan",
            "The project's watch list: the cursor, the merged rows in their window, the heads awaiting CI, the lane PRs "
            "whose commit list is still unknown, and the per-call caps.",
            scan.plan, refusals=((LookupError, 404, None),),
        ),
        ToolboxCall(
            "POST", "/api/agent/outcome-scan/report", "post_outcome_scan_report",
            "One page of facts, judged as it arrives; the `last` page moves the cursor. 422 only for a shape no page "
            "could mean; a malformed item is dropped, never refused.",
            scan.ingest, refusals=((scan.ReportInvalid, 422, None), (LookupError, 404, None)),
        ),
    ),
)
