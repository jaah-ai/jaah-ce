// The Workflows surface's API client, shared by both tabs and the detail page (follows `pages/cron/api.ts`).
// Rows are the server's projection: identity is curated in `app/workflow_registry.py`, counts are folded
// by `app/workflow_engine.py`. No browser-side copy of either.

import { get } from '../../api'
import { RUNS_PAGE_SIZE } from '../../hooks/usePagedList'

/* Every string vocabulary is typed `string` (as in `pages/cron/api.ts`): a row from an older/newer API
 * must render as itself. Readers look up with a fallback (`lib/dbosStatus.ts`, the row's `label`). */

/** ONE WORKFLOW TYPE — `routes/workflows.py::WorkflowTypeOut`.
 *  `name`…`queue` are registry facts, always true; `runs`…`last_status` are a bounded engine window, all
 *  zero/null both when nothing ran and when the engine was unreachable — `engine_available` tells them
 *  apart. */
export type WorkflowType = {
  /** The DBOS function name, and the only value the `name` filter accepts. */
  name: string
  label: string
  description: string
  /** What starts it, in words. */
  trigger: string
  /** The DBOS queue, or null when started directly (`DBOS.start_workflow`); only a queued workflow can sit
   *  ENQUEUED behind a limit. */
  queue: string | null
  /** Runs WITHIN THE SCAN WINDOW (`window`), never a lifetime total; every screen must say so. */
  runs: number
  /** `{status: count}` in the same window. SPARSE: absent, not zero. */
  by_status: Record<string, number>
  last_run_at: string | null
  last_status: string | null
}

/** `routes/workflows.py::WorkflowTypesOut` — every registry entry, in registry order, always. */
export type WorkflowTypesPayload = {
  workflows: WorkflowType[]
  /** How many recent runs the counts were folded from (`workflow_engine.AGGREGATE_SCAN_LIMIT`). */
  window: number
  /** False when DBOS was unreachable, so an outage does not render as an idle control plane. */
  engine_available: boolean
}

/** `routes/workflows.py::WorkflowDetailOut`, which extends the list row server-side so shared fields
 *  never disagree. */
export type WorkflowDetail = WorkflowType & {
  window: number
  engine_available: boolean
}

/** ONE RUN — `routes/workflows.py::WorkflowRunOut`. Both `name` (what the filter accepts) and `label`
 *  (what an operator reads) travel. */
export type WorkflowRun = {
  workflow_id: string
  name: string
  label: string
  /** DBOS's vocabulary; rendering is single-homed in `lib/dbosStatus.ts`. */
  status: string
  queue_name: string | null
  executor_id: string | null
  app_version: string | null
  /** 1 for a single run; ABOVE 1 means it was RECOVERED by another executor. */
  recovery_attempts: number | null
  /** Nullable instants. No `dequeued_at`: never reached a worker; no `completed_at`: not returned. Both
   *  null is a legitimate in-flight run. */
  created_at: string | null
  dequeued_at: string | null
  completed_at: string | null
  error: string | null
  /** Arguments as a Python `repr` cut at `workflow_engine.PAYLOAD_MAX_CHARS` (cut MARKED). Display text,
   *  not a structure; `./vocabulary.ts::taskIdOf` is the one reader and says why it is safe. */
  input: string | null
  /** The return value, same terms. For `task_done`, the per-action status map. */
  output: string | null
}

/** One OFFSET page of runs across all workflows, newest first. `next_offset` is null at the end. No total:
 *  the engine has no count-only call. */
export type WorkflowRunsPage = {
  runs: WorkflowRun[]
  limit: number
  offset: number
  next_offset: number | null
  engine_available: boolean
}

/* The server's cap: `routes/workflows.py` declares `Query(default=RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT)`
 * and 422s past it. Exported to be ASSERTED against (`WorkflowRunsTab.test.tsx` pins `RUNS_PAGE_SIZE <= RUNS_MAX_LIMIT`).
 * The offset ceiling is deliberately not mirrored: this bundle only echoes the server's `next_offset`;
 * `routes/workflows.py`'s `OFFSET_MAX` is its one home. */
export const RUNS_MAX_LIMIT = 200

/** Optional filters; `''` equals absent, translated here so a stray `?status=` (refused by the server)
 *  never goes on the wire. */
export type RunFilters = {
  name?: string | null
  status?: string | null
  /** A workflow-id PREFIX, not a substring (DBOS indexes by prefix). Every screen offering it must say so. */
  search?: string | null
  offset?: number
}

export const listWorkflowTypes = () => get<WorkflowTypesPayload>('/api/workflows')

/** One type's descriptor plus its rollup. 404 for a name the registry does not carry. */
export const getWorkflow = (name: string) =>
  get<WorkflowDetail>(`/api/workflows/${encodeURIComponent(name)}`)

/** `offset` is the server's `next_offset` echoed verbatim, never computed from `limit`. */
export function listWorkflowRuns(filters: RunFilters = {}) {
  const params = new URLSearchParams({ limit: String(RUNS_PAGE_SIZE) })
  if (filters.name) params.set('name', filters.name)
  if (filters.status) params.set('status', filters.status)
  if (filters.search) params.set('search', filters.search)
  if (filters.offset) params.set('offset', String(filters.offset))
  return get<WorkflowRunsPage>(`/api/workflows/runs?${params}`)
}

/** PENDING `cron_tick` runs (`sched-cron:*` ids), which the runs page excludes — `routes/workflows.py::
 *  list_pending_cron_ticks`. Bounded, not paged. */
export const listPendingCronTicks = () => get<WorkflowRunsPage>('/api/workflows/cron-ticks/pending')

/** One run by its exact id, in the runs page's row shape — a pasted run URL's load. 404 for an id the catalogue
 *  does not carry. */
export const getWorkflowRun = (workflowId: string) =>
  get<WorkflowRun>(`/api/workflows/runs/${encodeURIComponent(workflowId)}`)
