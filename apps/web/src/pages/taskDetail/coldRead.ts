// The Task page's COLD LOAD: one shared `GET …/page` read per id and org scope, and its per-route fallbacks.
import { ApiError, get } from '../../api'
import { currentOrgScope } from '../../lib/orgScope'
import type { TaskPageBundle, TaskPageTrail } from '../../lib/taskPageSeed'
import type { Artifact, PipelineTask } from '../../types'
import type { ActivityLogEntry } from './ActivityLog'

/* ── The COLD LOAD's read, once per id and scope ─────────────────────────────────────────────────────────────
   ONE `GET …/page` carries the task, its activity and artifacts and every panel's first read (`lib/taskPageSeed.ts`).
   An absent part reads its own GET. A `…/page` failure falls back to the three single reads (an API
   without the route, a 5xx) — EXCEPT a 404 carrying the task route's own refusal (`TASK_NOT_FOUND`): the task is
   gone or hidden, the three reads would only 404 again, so it is final.
   Shared while IN FLIGHT, keyed on the id and the org scope the request carries (`lib/orgScope.ts`): a deep link
   whose `?orgs=` the shell is still selecting mounts this page and then REMOUNTS it under the new scope's key
   (`App.tsx::AuthLayout`) while the first read is in flight, and StrictMode mounts twice. Each mount awaits the one
   read. Dropped once it lands, so a later visit reads afresh, and the moment an event that moves the page is heard
   (`forgetColdReads`), so the live re-read it schedules never answers from a read that predates it. */
type ColdRead = {
  bundle: TaskPageBundle | null
  detail: PipelineTask
  activity: ActivityLogEntry[] | null
  // `null` = no answer (5xx, 401/403, network); `[]` = none.
  artifacts: Artifact[] | null
  // The trail's names; `null` from a fallback read or an API that does not send them.
  trail: TaskPageTrail | null
}
const coldReads = new Map<string, Promise<ColdRead>>()

/* The task route's own 404 (`app/task_lookup.py::get_task_or_404`, and the object guard's `not found` in
   `app/authz/`), which `…/page` propagates. A route the API lacks answers FastAPI's own `Not Found`. */
const isTaskRouteNotFound = (err: unknown, id: string): boolean =>
  err instanceof ApiError && err.status === 404 && (err.detail === `task ${id} not found` || err.detail === 'not found')

/* Artifacts ride the SAME load because the tab's existence depends on them: a late tab would shift Details/Activity
   under a travelling pointer. The catch keeps an artifacts failure from failing the page. A 404 maps to `[]`:
   beside a detail that loaded it can only mean a row deleted between the two requests. */
export const readActivity = (id: string) => get<ActivityLogEntry[]>(`/api/tasks/${id}/activity`)
export const readArtifacts = (id: string) => get<Artifact[]>(`/api/tasks/${id}/artifacts`)
  .catch(err => (err instanceof ApiError && err.status === 404 ? [] : null))

async function readCold(id: string): Promise<ColdRead> {
  const bundle = await get<TaskPageBundle>(`/api/tasks/${id}/page`).catch(err => {
    if (isTaskRouteNotFound(err, id)) throw err
    return null
  })
  const [detail, activity, artifacts] = await Promise.all(bundle !== null
    ? [bundle.task, (bundle.activity as ActivityLogEntry[] | undefined) ?? readActivity(id), bundle.artifacts ?? readArtifacts(id)]
    : [get<PipelineTask>(`/api/tasks/${id}`), readActivity(id), readArtifacts(id)])
  return { bundle, detail, activity, artifacts, trail: bundle?.trail ?? null }
}

export function readColdOnce(id: string): Promise<ColdRead> {
  const key = `${id}|${(currentOrgScope() ?? []).map(org => org.id).join(',')}`
  const inFlight = coldReads.get(key)
  if (inFlight) return inFlight
  const read: Promise<ColdRead> = readCold(id).finally(() => {
    if (coldReads.get(key) === read) coldReads.delete(key)
  })
  coldReads.set(key, read)
  return read
}

export function forgetColdReads(id: string): void {
  for (const key of coldReads.keys()) if (key.startsWith(`${id}|`)) coldReads.delete(key)
}
