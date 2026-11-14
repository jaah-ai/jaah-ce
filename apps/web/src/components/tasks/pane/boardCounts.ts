import type { DateRangeBounds } from '../../../lib/taskDateRange'
import { scopeToListParams, type TaskScope } from '../../../lib/taskScope'
import { boardFilterParams } from '../shared'

/* THE STATE BOARD'S SECTION TOTALS (`GET /api/tasks/counts`), read under the board's own filters
 * (`boardFilterParams`), so a header and the rows under it describe one set (AC-6.5). */

export type PaneFilters = {
  showHidden: boolean
  bounds: DateRangeBounds
  assigneeIds: string[]
  /** Creator ids, EMPTY for every creator (`lib/taskUrlFilters.ts`). */
  creatorIds: string[]
  /** Source codes, EMPTY for every source (`lib/taskUrlFilters.ts`). */
  sources: string[]
  /** The State filter, `''` for every state (`lib/taskUrlFilters.ts`). Absent = every state. */
  state?: string
  /** The Persona filter, `''` for every persona (`lib/taskUrlFilters.ts`). Absent = every persona. */
  persona?: string
  focus: boolean
  q: string
  /** The user's muted nodes (`lib/taskMute.ts`): their rows leave every scope above them. Absent = none. */
  muted?: readonly string[]
}

/** `GET /api/tasks/counts` for the scope, under the list's own filters. */
export function paneCountsUrl(scope: TaskScope, filters: PaneFilters): string {
  const { muted, ...filter } = filters
  const query = boardFilterParams({ scopeFilter: scopeToListParams(scope, muted), ...filter }).toString()
  return `/api/tasks/counts${query ? `?${query}` : ''}`
}
