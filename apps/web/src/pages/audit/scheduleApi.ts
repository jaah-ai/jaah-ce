/* THE FLEET-WIDE CRON LEDGER'S OWN API CLIENT — every firing of every schedule, newest first.
 * The ROW TYPE IS SHARED with `pages/cron/api.ts`, not copied (`FleetCronRun`).
 */

import { get } from '../../api'
import { RUNS_PAGE_SIZE } from '../../hooks/usePagedList'
import { RUNS_MAX_LIMIT, type CronRun } from '../cron/api'

/** ONE FIRING, ACROSS EVERY SCHEDULE — `routes/crons.py`'s ledger row with the cron's identity on it.
 *
 *  DERIVED FROM `CronRun` (documented in `pages/cron/api.ts`, the only place),
 *  PLUS `cron_id` (the address and filter value) and `cron_name` (what an operator reads). */
export type FleetCronRun = CronRun & {
  cron_id: string
  cron_name: string
}

/** One KEYSET page of the ledger, newest first — `GET /api/crons/runs`.
 *
 *  `next_before` is the cursor for the following page, or null at the end, ECHOED BACK VERBATIM (the
 *  server refuses a cursor it did not mint). DELIBERATELY NO TOTAL (`routes/crons.py::FleetCronRunsPage`).
 *  `crons` and `outcomes` ARE THE FILTER VOCABULARIES from the server, never the loaded rows, which would
 *  hide a QUIET schedule. */
export type FleetCronRunsPage = {
  runs: FleetCronRun[]
  limit: number
  next_before: string | null
  /** Every cron the reader may filter by — a picker vocabulary. */
  crons: { id: string; name: string }[]
  /** `app/crons.py::RUN_OUTCOMES`, from the server; rendering is `pages/cron/vocabulary.ts`'s. */
  outcomes: string[]
  /** Enabled crons with `failing_threshold`+ errors since their last success, worst first. Unfiltered. */
  failing: { id: string; name: string; consecutive_errors: number }[]
  /** `routes/crons.py::FAILING_STREAK_THRESHOLD`, from the server. */
  failing_threshold: number
}

/* THE CEILING, IMPORTED: `routes/crons.py`'s `RUNS_MAX_LIMIT` (past the cap is a 422), already mirrored once in
 * `pages/cron/api.ts`. Re-exported for the panel's test. */
export { RUNS_MAX_LIMIT }

/** What narrows the ledger: the SERVER's vocabularies. `''` equals absent (an empty `outcome=` is a 422). */
export type ScheduleFilters = {
  /** A cron UUID, never a name. */
  cron?: string | null
  outcome?: string | null
  /** The server's `next_before`, echoed verbatim. */
  before?: string | null
}

export function listFleetCronRuns(filters: ScheduleFilters = {}) {
  const params = new URLSearchParams({ limit: String(RUNS_PAGE_SIZE) })
  if (filters.cron) params.set('cron', filters.cron)
  if (filters.outcome) params.set('outcome', filters.outcome)
  if (filters.before) params.set('before', filters.before)
  return get<FleetCronRunsPage>(`/api/crons/runs?${params}`)
}
