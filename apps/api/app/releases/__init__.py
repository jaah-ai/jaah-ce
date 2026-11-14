"""Releases: moving a project's code from one branch stage to the next — Dev (the integration branch) → … → Production.

A **Release** is a request to move one stage's branch head into the next stage's branch (one press of Release = one step,
k → k+1). The control plane owns the request — who may ask (`access.py`), approvals, schedule, freezes
(`freezes.py`), the changes it carries (`waiting.py`) — and a hidden `release` executor task on a node carries it out
(`executor.py::ensure_executor`; the node script and its verbs are the executor's own). Whether it went live is the
`deployments` ledger's fact, linked by `releases.deployment_id`, never copied.

Every project with a repo has its pipeline with no configuration — Ideas, Building, Dev, Production
(`stages.py::materialise`); releases run between its branch-level stages (`stages.py::branch_stages`), each on the
settings it was requested with (`snapshot.py`). The setup API is `pipeline.py`.
Production's `deploy_environment` is the single home of the environment name the ledger matches
(`stages.py::production_environment`); `projects.production_environment` is no longer read or written.

Modules: `lifecycle.py` (states and the transition table), `stages.py`, `pipeline.py`, `snapshot.py`, `freezes.py`, `access.py`, `waiting.py`,
`executor.py`, `jobs.py` (survey and connect jobs), `service.py` (the request verbs), `views.py` (the API shapes),
`tick.py` (the `release-tick` cron: schedules, auto-deploy, backstops), `notify.py` (approval, Live, failed),
`anchoring.py` (the ledger's side: learning, addresses, anchors), `live_link.py` (the task page's link).
Routes: `app/routes/releases.py`.
"""
from __future__ import annotations

from typing import Any


def ensure_executor(*args: Any, **kwargs: Any) -> Any:
    """`app/releases/executor.py::ensure_executor` (deferred: model modules import this package's lifecycle)."""
    from app.releases.executor import ensure_executor as _ensure

    return _ensure(*args, **kwargs)
