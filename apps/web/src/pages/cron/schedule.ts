/* THE SCHEDULE DRAFT — what the Settings tab edits, the one shape crossing between page and editor.
 *
 * IT COMPUTES NOTHING ABOUT TIME: parsing, next-fire and interval→expression live once in
 * `app/cron_schedule.py`, reached via `POST /api/crons/preview`. This owns only SHAPE: the kind tokens,
 * the draft, the saved fields, the number↔unit split. (`pages/cron/vocabulary.ts` owns the RUN vocabulary.)
 */

/* The two kinds `app/crons.py::SCHEDULE_KINDS` declares, compared against a row `string` that may hold a
 * kind this build does not know. */
export const SCHEDULE_KIND_CRON = 'cron'
export const SCHEDULE_KIND_INTERVAL = 'interval'

/* THE ZONE AN INTERVAL SCHEDULE IS EVALUATED IN — always UTC: a duration has no wall clock
 * (`app/cron_schedule.py::next_fire_for_row`; server name `routes/crons.py::INTERVAL_TIMEZONE_NAME`).
 * Named once so the kind hint and the summary caption cannot disagree. */
export const INTERVAL_TIMEZONE_NAME = 'UTC'

/** The Settings tab's draft of WHEN this cron fires. BOTH COLUMNS ARE HELD, so switching kind and back
 *  keeps the expression; `scheduleFields` decides what is SENT. */
export type ScheduleDraft = {
  kind: string
  /** Never null: `''` is "nothing typed yet", refused by name on save. */
  expression: string
  /** Null means the field was emptied — not zero, which is not an interval. */
  intervalSeconds: number | null
}

/** The kind and its own column, unconditionally (`ck_crons_exactly_one_schedule`: exactly one column). */
export function scheduleFields(draft: ScheduleDraft): Record<string, unknown> {
  if (draft.kind === SCHEDULE_KIND_INTERVAL) {
    return { schedule_kind: draft.kind, interval_seconds: draft.intervalSeconds }
  }
  return { schedule_kind: draft.kind, cron_expression: draft.expression.trim() }
}

/** Seconds, split into the count and unit an operator reads it as. */
export type IntervalUnits = {
  /** Null when there is no interval yet (renders empty, not `0`). */
  count: number | null
  /** The unit's size in seconds: 1, 60, 3600 or 86400. */
  unit: number
}

/* Largest first: the BIGGEST unit dividing exactly (3600 -> "1 hour"). Anything else shows as seconds,
 * never rounded. */
const UNIT_SIZES = [86400, 3600, 60, 1] as const

export function intervalUnits(seconds: number | null): IntervalUnits {
  if (seconds === null) return { count: null, unit: 1 }
  const unit = UNIT_SIZES.find(size => seconds % size === 0 && seconds >= size) ?? 1
  return { count: seconds / unit, unit }
}
