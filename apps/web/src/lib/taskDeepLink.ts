// The Title → task deep link (BA R9). One home for its strings: `components/WorktreePool.tsx` composes the
// href, `/tasks` (Tasks.tsx) consumes it, and the tree table emits the anchor for both row levels — so the
// link never dead-ends (BA AC9.2). Targets the tasks LIST, not `/tasks/:id` (BA D8).

import { STAGE_PARAM } from './pipelineBoard'
import { SCOPE_PARAM } from './taskScope'

// The param carrying the task to reveal.
export const TASK_DEEP_LINK_PARAM = 'task'

/* The optional project param: a bare `?task=` widens the board to All projects; a link naming the project
   lands on that project's board. Carries a project id. */
export const TASK_DEEP_LINK_PROJECT_PARAM = 'project'

/* `scope` (a `lib/taskScope.ts::scopeKey`) is the board selection to KEEP: the task editor returns to the
   scope the operator left, and `/tasks` then leaves `?scope=` alone — even when the task is not in
   it. It wins over `projectId`, which is the fallback when the tab has no earlier scope.
   `projectId` and `scope` omitted or null → the bare link. `stage` (a `lib/pipelineBoard.ts::STAGE_PARAM` value)
   opens the board on that stage; `/tasks` keeps it, as it keeps `?scope=`. */
export function taskDeepLinkPath(
  taskId: number, projectId?: number | null, scope?: string | null, stage?: string | null,
): string {
  const base = `/tasks?${TASK_DEEP_LINK_PARAM}=${taskId}`
  const where = scope
    ? `&${SCOPE_PARAM}=${encodeURIComponent(scope)}`
    : isPositiveInteger(projectId) ? `&${TASK_DEEP_LINK_PROJECT_PARAM}=${projectId}` : ''
  return stage ? `${base}${where}&${STAGE_PARAM}=${encodeURIComponent(stage)}` : `${base}${where}`
}

// The row's DOM id, emitted by every task row (distinct from the retired
// `task-detail-<id>`, which anchored below the row).
export function taskRowElementId(taskId: number): string {
  return `task-row-${taskId}`
}

// A positive integer or null — validated, never coerced (`?task=abc` must not `GET /api/tasks/NaN`).
export function parseTaskDeepLinkParam(raw: string | null): number | null {
  // Decimal digits only, as `lib/taskScope.ts::parseScope` reads a project id: `Number()` alone would take
  // `1e2` or `0x10`.
  const trimmed = raw?.trim() ?? ''
  if (!/^[1-9][0-9]*$/.test(trimmed)) return null
  const id = Number(trimmed)
  return Number.isSafeInteger(id) ? id : null
}

// The project read back, or null when absent, blank or not a positive integer — an older link carrying
// something else widens to All projects rather than erroring.
export function parseTaskDeepLinkProjectParam(raw: string | null): number | null {
  return parseTaskDeepLinkParam(raw)
}

function isPositiveInteger(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}
