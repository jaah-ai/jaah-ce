"""The `/api/agent/*` surface a node daemon and its runners call, one domain per module.

Each module carries its own `APIRouter`; `app/main.py` includes them in a fixed order.

- `auth`: agent-route authentication and authorization: who the calling node is, which task it declares, and what it may touch.
- `accounts_worktrees`: agent reads of the node's accounts and pool worktrees (`GET /api/agent/accounts`, `/worktrees`).
- `pool_tick`: the node's pool tick: slot roster, known slots, pending intents and port grid (`GET /api/agent/pool-tick`).
- `slot_intents`: slot intents and holds reported by the node: intent ack, launch claim, slot holder and user-session end.
- `fleet_observe`: fleet observation the node reports: observe passes, alerts and run telemetry.
- `node_config`: node self-reports and self-reads: config snapshot, egress, logs, model catalogs, disk reclaims, liveness and identity.
- `releases`: what the CP wants a node to run: release, autoupdate, runner flags, port grid and NodeLink mounts.
- `gha_runners`: the node's GitHub Actions runner pool: desired slots, JIT registration and observed-state reports.
- `task_reads`: agent reads and claims of tasks: by id, attachments, bundle, claim, system tasks and needs-attention.
- `runner_liveness`: runner liveness: breaks, heartbeats, and console exit/resume.
- `task_terminal`: a runner's task outcomes: settle, fail, PRs, questions, runner session, appended tasks and follow-ups.
- `agent_filing`: tasks an agent files outside its own pipeline: top-level drafts and edits/deletes of its filings.
- `task_secrets`: secrets and metered resources a task's runner fetches: git tokens, credentials, API keys, Bedrock and usage.
- `project_memory`: project memory's capture and report legs (`/tasks/{task_id}/memory/ingest`, `/report`).
"""
