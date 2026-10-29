"""Durable workflows — the control plane's long-running, human-in-the-middle flows.

A flow belongs here when it must outlive the process that started it: it waits on a human or a node
longer than a request can be held open, and a redeploy mid-wait must not lose it. The runtime is
`app/durable/runtime.py`.

Work that merely runs on a cadence is a `crons` row instead. The admission test: is losing
the work mid-flight recoverable by running it again later? A sweep is; a login that already showed a
one-time URL is not, nor is an outbound alert nobody re-sends, nor a title owed once by the create
that made the row. `task_done.py` is here because its three actions had no shared record of progress.
A row still sitting there gets a sweep; an event that has already passed gets a queue.

UPGRADE RUNBOOK — a DBOS release that changes `durable_dbos_sys` storage deploys STOP-THEN-START, never
overlapped with the old release (an old-revision task would dequeue from the new layout):
1. Back up the schema (`pg_dump -Fc -n durable_dbos_sys`, kept by the operator only — it holds login state).
2. SUSPEND the CP's autoscaling if it is on (`aws application-autoscaling register-scalable-target` on
   `service/<cluster>/<service>` with every `--suspended-state` flag true; its floor would otherwise refill the
   service), scale the service to 0 and wait until its tasks are STOPPED (`aws ecs wait tasks-stopped`;
   `services-stable` returns while they still drain and tick).
3. Merge (dev) or promote (main). THE APPLY STARTS NOTHING: `aws_ecs_service.cp` ignores `desired_count` (the
   scaler owns it, in the deployment's autoscaling Terraform), so it only swaps the task definition of
   an empty service. Raise it by hand (`aws ecs update-service --desired-count 2`) once that swap lands —
   inside the 600s the deploy's rollout wait allows for a healthy task, or that step fails. Resume the scaler
   last.
4. Watch `aws ecs list-tasks` through the apply; an old-revision task that does appear must be `stop-task`ed at
   once, and any workflow it dequeued from the new layout fails and is left PENDING (3.x recovers only at
   startup), so cancel those by id afterwards.
The `application_version` pin stays, so the new release recovers every PENDING row and dequeues every ENQUEUED
one the old one left; bumping it would orphan them. Rollback is forward-fix; restoring the backup (and
reverting) is the last resort. A migration slow enough to fail the health check runs out of band first
(`dbos migrate --schema durable_dbos_sys`).
"""
