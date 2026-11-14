"""The release executor's calls: its next release or job (`release.next`), what happened to it (`release.event`), and
jaah-forge's question whether a release PR is the caller's to open or merge (`release.authorize`). The callers are
`tools/release.py` and `jaah-forge` on a node, through the daemon's TASK-scoped verbs, never the model (the door and
the refusals: `routes/agent/toolbox.py`). `next` and `event` answer an open `release` task only; `authorize` answers
the release run and the merge lane run carrying a release PR, and the same 404 to everything else. The rules are
`app/releases/runs.py`'s.
"""
from __future__ import annotations

from app.releases import executor, runs
from app.routes.agent.toolbox import ToolboxCall, toolbox_router

_NOT_FOUND = "no release run for this task"

router = toolbox_router(
    tag="agent-release",
    not_found=_NOT_FOUND,
    persona=executor.PERSONA,
    calls=(
        ToolboxCall(
            "GET", "/api/agent/release/next", "get_release_next",
            "The run's next release or executor job, claimed; `{\"mode\": null}` when nothing is due.",
            runs.next_work, refusals=((LookupError, 404, None), (runs.Conflict, 409, None)), caps=True,
        ),
        ToolboxCall(
            "POST", "/api/agent/release/events", "post_release_event",
            "One executor event on a release or job: 404 for one this project does not hold, 409 for one that moved "
            "on (drop it), 422 for a shape no event means.",
            runs.apply_event,
            refusals=((runs.Conflict, 409, None), (LookupError, 404, None), (ValueError, 422, None),
                      (KeyError, 422, None)),
        ),
    ),
)

authorize_router = toolbox_router(
    tag="agent-release",
    not_found=_NOT_FOUND,
    persona=None,
    calls=(
        ToolboxCall(
            "POST", "/api/agent/release/authorize", "post_release_authorize",
            "Whether this run may open or merge the release PR `head` → `base` (an open release names both), or, with "
            "`purpose: dispatch` and no `base`, start a CI run on the pinned branch `head`.",
            runs.authorize, refusals=((LookupError, 404, None),),
        ),
    ),
)
router.include_router(authorize_router)
