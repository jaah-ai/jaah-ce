/* The `/tasks` board's DATE RANGE vocabulary — the five choices and the bounds each resolves to. Pure of
 * React (like `taskFilterStorage.ts`): this owns what each choice MEANS, the half a clock, a timezone or
 * a DST boundary can break silently.
 *
 * IT FILTERS ON LAST ACTIVITY (`updated_at`) ONLY — the operator's decision; do not widen it to
 * `created_at` (a year-old task running now would vanish).
 *
 * THE WORDING IS FIXED AND NON-NUMERIC (Today / Recent / Wider / Exact dates, plus off) — operator's
 * choice. The day count moves into the `hint` tooltip and `dateRangeSummary`.
 *
 * EVERY WINDOW IS CALENDAR-DAY ALIGNED: a rolling `now - 7 * 24h` floor drops this morning's rows during
 * the day. Midnights are the CONTROL PLANE'S CONFIGURED ZONE (Settings > General >
 * Timezone), resolved by `lib/officialZone.ts` and passed in as `zone`; `lib/zonedDay.ts` is the single
 * home for the arithmetic. Why not the viewer's zone: the Activity Feed's recency headings
 * (`components/tasks/listViews/rows.ts`) bucket on the configured zone, and two operators must get one
 * answer from one board. The accepted cost: a far-away viewer's "Today" is not their local today.
 *
 * Days move by DAY NUMBER (`addZonedDays`), never `-n * 86400000`, which misses midnight across DST.
 */
import {
  addZonedDays,
  calendarDate,
  endOfZonedDay,
  startOfZonedDay,
  zonedDayFromInput,
  zonedIso,
} from './zonedDay'

export type TaskDateRangeId = 'all' | 'today' | 'recent' | 'wider' | 'exact'

export type TaskDateRange = {
  id: TaskDateRangeId
  /** The link's own text (non-numeric; see above). */
  label: string
  /** The link's `title`, including the day count the label omits. */
  hint: string
}

/* Panel order is a widening gesture: off, narrowest, wider, wider, then the manual escape hatch. */
export const TASK_DATE_RANGES: readonly TaskDateRange[] = [
  { id: 'all', label: 'Any time', hint: 'No date limit.' },
  { id: 'today', label: 'Today', hint: 'Anything that moved since midnight.' },
  { id: 'recent', label: 'Recent', hint: 'The past 7 days.' },
  { id: 'wider', label: 'Wider', hint: 'The past 30 days.' },
  { id: 'exact', label: 'Exact dates', hint: 'Pick a start and an end.' },
] as const

/** The unfiltered board: the default and the fallback for anything unreadable HIDES NOTHING. */
export const DEFAULT_TASK_DATE_RANGE: TaskDateRangeId = 'all'

/** Validate a stored/untrusted value; garbage falls back to "no date limit". */
export function isTaskDateRangeId(value: unknown): value is TaskDateRangeId {
  return typeof value === 'string' && TASK_DATE_RANGES.some(range => range.id === value)
}

/**
 * The window for the wire: ISO-8601 instants WITH THE CONFIGURED ZONE'S OFFSET (the server parses in its
 * own zone, so an offset-less string is ambiguous), each key ABSENT rather than `''` when unbounded, so
 * `buildTasksUrl` never sends a param nobody asked for.
 */
export type DateRangeBounds = { updatedAfter?: string; updatedBefore?: string }

/**
 * The bounds a choice resolves to, `{}` meaning "no date limit".
 *
 * `zone` is the CP's configured zone, a PARAMETER so this stays pure and tests can pin it. `now` is
 * injectable for tests.
 *
 * `{}` for every unusable input: `all`, empty `exact`, an unparseable date, or an INVERTED pair —
 * swapping would apply a window nobody typed, passing through would show zero rows ("no work").
 * `dateRangeSummary` says in words that the start is after the end.
 */
export function resolveDateRangeBounds(
  id: TaskDateRangeId,
  from: string,
  to: string,
  zone: string,
  now: Date = new Date(),
): DateRangeBounds {
  const today = startOfZonedDay(now, zone)
  switch (id) {
    case 'today':
      // No upper bound: "since midnight".
      return { updatedAfter: zonedIso(today, zone) }
    case 'recent':
      // MINUS SIX: 7 calendar days INCLUDING TODAY.
      return { updatedAfter: zonedIso(addZonedDays(today, -6, zone), zone) }
    case 'wider':
      return { updatedAfter: zonedIso(addZonedDays(today, -29, zone), zone) }
    case 'exact': {
      const start = from === '' ? null : zonedDayFromInput(from, zone)
      const end = to === '' ? null : zonedDayFromInput(to, zone)
      // Typed-but-unparseable is NOT blank: a half-typed `2026-08-` must not open the other bound.
      if (from !== '' && start === null) return {}
      if (to !== '' && end === null) return {}
      if (start !== null && end !== null && start.getTime() > end.getTime()) return {}
      const bounds: DateRangeBounds = {}
      if (start !== null) bounds.updatedAfter = zonedIso(start, zone)
      // THE END OF THE `to` DAY (23:59:59.999, configured zone): a date names a DAY, so `from === to`
      // must mean that entire day, not a zero-width instant.
      if (end !== null) bounds.updatedBefore = zonedIso(endOfZonedDay(end, zone), zone)
      return bounds
    }
    default:
      return {}
  }
}

/** Whether a `<input type="date">` value is usable. `''` IS valid (the input's own "unset"); anything
 *  else must be a real `YYYY-MM-DD`, so `2026-02-31` is rejected, not rolled into March.
 *
 *  NO `zone`: whether a string names a real day is zone-free (and `taskFilterStorage.ts` calls this with
 *  no zone in hand). Uses `zonedDay.ts::calendarDate`, the same check `zonedDayFromInput` runs. */
export function isDateInput(value: string): boolean {
  return value === '' || calendarDate(value) !== null
}

/**
 * Whether this choice actually REMOVES ROWS (the toolbar badge). Derived from `resolveDateRangeBounds`
 * so an empty or inverted `exact` is not active.
 */
export function dateRangeIsActive(id: TaskDateRangeId, from: string, to: string, zone: string): boolean {
  const bounds = resolveDateRangeBounds(id, from, to, zone)
  return bounds.updatedAfter !== undefined || bounds.updatedBefore !== undefined
}

/**
 * ONE SHORT LINE describing the window in force — the caption under the control; the SENTENCE register
 * of `dateRangePeriodLabel`. Spells out the day count and says what is wrong with an unusable `exact`.
 * NO `zone`: these describe the inputs; which midnight is `resolveDateRangeBounds`'s business.
 */
export function dateRangeSummary(id: TaskDateRangeId, from: string, to: string): string {
  switch (id) {
    case 'today':
      return 'Today only — anything that moved since midnight.'
    case 'recent':
      return 'The past 7 days, today included.'
    case 'wider':
      return 'The past 30 days, today included.'
    case 'exact': {
      if (from === '' && to === '') return 'Pick a start or an end — no date limit yet.'
      if (!isDateInput(from) || !isDateInput(to)) return 'That is not a date yet — no date limit applied.'
      // In words, because the bounds come back empty and would look like no range at all.
      if (from !== '' && to !== '' && from > to) {
        return `Start ${from} is after the end ${to} — no date limit applied.`
      }
      if (from !== '' && to !== '') return `${from} to ${to}, both days included.`
      if (from !== '') return `${from} onwards.`
      return `Up to and including ${to}.`
    }
    default:
      return 'Any time — no date limit.'
  }
}

/**
 * THE SAME WINDOW, AS A TITLE — a noun phrase for the Period chip (a caption sentence reads as UI help). An `exact` pair resolving to NO bounds titles "All time", which is what
 * the board shows.
 */
export function dateRangePeriodLabel(id: TaskDateRangeId, from: string, to: string): string {
  switch (id) {
    case 'today':
      return 'Today'
    case 'recent':
      return 'The past 7 days'
    case 'wider':
      return 'The past 30 days'
    case 'exact': {
      // ASKED IN UTC because the question is zone-free (are the dates real, is start <= end), routed
      // through `dateRangeIsActive` so the rule is stated once. A clock-dependent branch here would need a
      // real `zone` parameter.
      if (!dateRangeIsActive(id, from, to, 'UTC')) return 'All time'
      if (from !== '' && to !== '') return `${from} to ${to}`
      if (from !== '') return `${from} onwards`
      return `Up to ${to}`
    }
    default:
      return 'All time'
  }
}

