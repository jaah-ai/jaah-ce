"""Console Task CRUD + fleet-dispatch action routes, one domain per module.

A human authors work as a `draft` epic Task, starts it (`start`), or answers a runner's question; every route is
`require_user`-gated. `apps/api` never calls `gh`/git, so the action routes are DB-direct. Each
module declaring routes has its own `router` on `/api/tasks`; `app/main.py` includes them in a fixed order.

Modules:
- `policy` — Task policy: operator-tunable caps and timeouts, the persona vocabulary and per-persona defaults.
- `qa_render` — Question and Q&A-trail rendering: option details, question rows and the prompt's fitted trail.
- `task_wire` — The Task wire shape: `TaskOut` and the shared Out models, and the readers that fill them.
- `listing` — Task listing: search, the board, list, facets, tree, counts and the persona registry.
- `slots` — Worktree slots a Task holds: bind/release, runner kill intents and sending minted intents.
- `dependencies` — Task dependencies: the edge routes, cycle and deadlock checks, and staleness propagation.
- `create` — Task creation: the create route, its spec validation, playbook plans, epic children and body versions.
- `dispatch` — Task dispatch: the dispatchable queue, claim, and Start (`_start`, `_start_epic`).
- `epics` — Epic fan-out and rollup: appending children, failure re-evaluation, completion and the stuck scan.
- `epic_finish` — Epic finish: the finish phase, its follow-ups, the final report and acceptance checks.
- `runner_settle` — Runner attempt settlement: end_attempt, charges, reassert/resume, reclaim and node-fault refunds.
- `awaiting` — Awaiting-response settle and expiry: parking on a runner question, resuming, timing it out.
- `update` — Task update: `PUT /api/tasks/{task_id}`, its `_apply_*` edits, and tags/retag.
- `title` — Task title: rename and Re-generate title, on the caller's plane (an operator renames any org's task).
- `delete` — Task deletion: `DELETE /api/tasks/{task_id}` and the cascade it runs.
- `agent_filing` — Agent-filed top-level Tasks: filing, dedup, and editing or deleting an agent's own drafts.
- `read` — Task read: `GET /api/tasks/{task_id}`.
- `answers` — Answering a Task's questions: delivery to the live runner, Slack echo, rounds, ignore/revise, the answer lock.
- `transitions` — Task state transitions: priority, reset, stop, wait-on-customer, set-state, force-start, complete.
- `activity` — Task activity: the activity log, the questions list and run metrics.
"""
