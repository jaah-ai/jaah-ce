/* The `/tasks` board's ROW-REMOVING filters and their URL (de)serialisation, so any narrowed board is a
 * link. The URL is the source of truth, read through `useSearchParams` with no mirrored state; the
 * argument is single-homed in `lib/worktreeFilters.ts`. The rest of the board's URL — the selection
 * (`?scope=`, `lib/taskScope.ts`) and the grouping (`?group=`, `lib/taskGrouping.ts`) — has its own homes; Focus alone stays in `lib/taskFilterStorage.ts`.
 *
 * Every reader is total: a URL is hand-edited, truncated and rewritten, so an unrecognised value is not
 * a filter — never an error, never a board filtered to nothing. Pure of React and the API client.
 */
import { uniqueNonEmpty } from './strings'
import { DEFAULT_TASK_DATE_RANGE, isDateInput, isTaskDateRangeId, type TaskDateRangeId } from './taskDateRange'
import { KNOWN_TASK_STATES } from '../generated/taskTransitions'

/**
 * The board's row-removing filter state — exactly what the URL carries and what the toolbar badge
 * counts (`components/tasks/TaskToolbar.tsx`). Anything that only changes how rows are drawn is not here.
 */
export type TaskUrlFilters = {
  /** Free text, matched SERVER-SIDE as `?q=` (every word, case-insensitive and in any order, against
   *  title, description and outcome, plus task id when numeric) — so it finds rows outside the lazy window. Stored verbatim;
   *  whitespace-only is blank everywhere (the server strips it), so `?q=+++` never sits over a `0` badge. */
  q: string
  /** The one ADDITIVE tick (hidden system rows + archived); still badged, since it changes which rows
   *  show. `buildTasksUrl` in `components/tasks/shared.tsx` owns the request params it sends. */
  showHidden: boolean
  /** Window over LAST ACTIVITY; `lib/taskDateRange.ts` owns the ids. `'all'` is off. */
  dateRange: TaskDateRangeId
  /** `Exact dates` bounds (`YYYY-MM-DD`, `''` unset). Kept and emitted even under a preset so switching
   *  back does not blank what was typed. */
  dateFrom: string
  dateTo: string
  /** Assignee ids; EMPTY means every assignee. Several picks are a UNION (repeatable `assigned_to_id=`). */
  assigneeIds: string[]
  /** Creator ids (`GET /api/tasks/creators`, whose "System" id also matches rows with no recorded creator); EMPTY
   *  means every creator. A UNION, like assignees (repeatable `creator=`). */
  creatorIds: string[]
  /** Origin codes (`GET /api/tasks/sources`); EMPTY means every source. A UNION, like assignees (repeatable `source=`). */
  sources: string[]
  /** One task state (`?state=` on `GET /api/tasks`), `''` for every state — so a bulk verb can
   *  reach exactly one project's drafts. A state the lifecycle does not name reads as `''`. */
  state: string
  /** One persona slug (`?persona=` on `GET /api/tasks`), `''` for every persona. Single-valued: the API takes
   *  one. Labelled by `components/tasks/personaRegistry.ts::personaLabel`, never shown as the slug. */
  persona: string
  /** An EXACT window over last activity, as ISO instants WITH an offset (`''` unset), sent verbatim as
   *  `updated_after`/`updated_before` (both inclusive). What the command center's count links carry; while
   *  either is set it overrides the Period choice (`lib/taskDateRange.ts::instantWindowBounds`). An inverted
   *  pair reads as unset, like an inverted `Exact dates` pair. */
  windowAfter: string
  windowBefore: string
}

/* One frozen empty array for every read with no picks: the page keys fetch effects on this value and
   re-reads on every query-string change (e.g. the `?task=` strip), so a fresh `[]` would refetch for
   nothing. */
const NO_ASSIGNEES: string[] = []
Object.freeze(NO_ASSIGNEES)
const NO_SOURCES: string[] = []
Object.freeze(NO_SOURCES)
const NO_CREATORS: string[] = []
Object.freeze(NO_CREATORS)

/* Param names must not collide with `lib/taskDeepLink.ts`'s `task`/`project`, which are stripped on
   arrival and would erase a same-named filter. Constants shared by reader and writer: the writer DELETES
   these keys off the caller's params, so both sides must name the same strings. */
const Q_PARAM = 'q'
const HIDDEN_PARAM = 'hidden'
const PERIOD_PARAM = 'period'
const FROM_PARAM = 'from'
const TO_PARAM = 'to'
const ASSIGNEE_PARAM = 'assignee'
const SOURCE_PARAM = 'source'
const CREATOR_PARAM = 'creator'
const STATE_PARAM = 'state'
const PERSONA_PARAM = 'persona'
const AFTER_PARAM = 'after'
const BEFORE_PARAM = 'before'

/** Every param this module owns: the board's row filters (`lib/taskFilterStorage.ts` remembers them per scope). */
export const OWNED_PARAMS = [
  Q_PARAM, HIDDEN_PARAM, PERIOD_PARAM, FROM_PARAM, TO_PARAM, ASSIGNEE_PARAM, CREATOR_PARAM, SOURCE_PARAM,
  STATE_PARAM, PERSONA_PARAM, AFTER_PARAM, BEFORE_PARAM,
]

/* A persona slug as the registry spells them (`apps/api/app/persona_registry.py`); anything else is no filter. */
const PERSONA_SLUG = /^[a-z][a-z0-9_-]{0,63}$/

/* An ISO-8601 instant that names its offset: an offset-less one would mean the viewer's zone here and UTC on
   the server. Seconds and fractions optional. */
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})$/

/* Strictly `1`; anything else is off (the wider board). */
const HIDDEN_ON = '1'

/**
 * Read the filters out of a URL. Total, and every fallback WIDENS: a garbled value is simply not a filter.
 */
export function readTaskUrlFilters(searchParams: URLSearchParams): TaskUrlFilters {
  const period = searchParams.get(PERIOD_PARAM)
  const dateInput = (key: string): string => {
    const raw = searchParams.get(key)
    return raw !== null && isDateInput(raw) ? raw : ''
  }
  const assigneeIds = uniqueNonEmpty(searchParams.getAll(ASSIGNEE_PARAM))
  const creatorIds = uniqueNonEmpty(searchParams.getAll(CREATOR_PARAM))
  const sources = uniqueNonEmpty(searchParams.getAll(SOURCE_PARAM))
  const state = searchParams.get(STATE_PARAM) ?? ''
  const persona = searchParams.get(PERSONA_PARAM) ?? ''
  let windowAfter = readInstant(searchParams.get(AFTER_PARAM))
  let windowBefore = readInstant(searchParams.get(BEFORE_PARAM))
  if (windowAfter !== '' && windowBefore !== '' && Date.parse(windowAfter) > Date.parse(windowBefore)) {
    windowAfter = ''
    windowBefore = ''
  }
  return {
    q: searchParams.get(Q_PARAM) ?? '',
    showHidden: searchParams.get(HIDDEN_PARAM) === HIDDEN_ON,
    dateRange: isTaskDateRangeId(period) ? period : DEFAULT_TASK_DATE_RANGE,
    dateFrom: dateInput(FROM_PARAM),
    dateTo: dateInput(TO_PARAM),
    assigneeIds: assigneeIds.length === 0 ? NO_ASSIGNEES : assigneeIds,
    creatorIds: creatorIds.length === 0 ? NO_CREATORS : creatorIds,
    sources: sources.length === 0 ? NO_SOURCES : sources,
    state: isTaskStateFilter(state) ? state : '',
    persona: PERSONA_SLUG.test(persona) ? persona : '',
    windowAfter,
    windowBefore,
  }
}

/* A hand-typed `+01:00` arrives as ` 01:00` (`+` is a space in a query string), so that one spelling is
   repaired; anything else that is not an offset-bearing instant is no bound. */
function readInstant(raw: string | null): string {
  if (raw === null) return ''
  const value = raw.replace(/ (\d{2}:?\d{2})$/, '+$1')
  return isIsoInstant(value) ? value : ''
}

/** Whether `value` is an ISO-8601 instant with an offset that names a real moment. */
export function isIsoInstant(value: string): boolean {
  return ISO_INSTANT.test(value) && !Number.isNaN(Date.parse(value))
}

/**
 * Write the filters into a URL, emitting only keys that narrow something.
 *
 * PATCHES the caller's params (unlike `writeWorktreeFilters`): `?task=`/`?project=` belong to
 * `lib/taskDeepLink.ts` and are consumed a render later, so returning only owned keys would delete a
 * pending deep link. A cleared filter leaves no trace; `q` is tested with `.trim()` but written verbatim.
 * Date bounds are emitted whenever set. Emission order is fixed so equal states yield equal query strings.
 */
export function writeTaskUrlFilters(
  filters: TaskUrlFilters,
  /** The page's current params; unowned keys survive. */
  base: URLSearchParams,
): URLSearchParams {
  const params = new URLSearchParams(base)
  for (const key of OWNED_PARAMS) params.delete(key)
  if (filters.q.trim() !== '') params.set(Q_PARAM, filters.q)
  if (filters.showHidden) params.set(HIDDEN_PARAM, HIDDEN_ON)
  if (filters.dateRange !== DEFAULT_TASK_DATE_RANGE && isTaskDateRangeId(filters.dateRange)) {
    params.set(PERIOD_PARAM, filters.dateRange)
  }
  if (filters.dateFrom !== '' && isDateInput(filters.dateFrom)) params.set(FROM_PARAM, filters.dateFrom)
  if (filters.dateTo !== '' && isDateInput(filters.dateTo)) params.set(TO_PARAM, filters.dateTo)
  for (const id of uniqueNonEmpty(filters.assigneeIds)) params.append(ASSIGNEE_PARAM, id)
  for (const id of uniqueNonEmpty(filters.creatorIds)) params.append(CREATOR_PARAM, id)
  for (const source of uniqueNonEmpty(filters.sources)) params.append(SOURCE_PARAM, source)
  if (isTaskStateFilter(filters.state)) params.set(STATE_PARAM, filters.state)
  if (PERSONA_SLUG.test(filters.persona)) params.set(PERSONA_PARAM, filters.persona)
  if (isIsoInstant(filters.windowAfter)) params.set(AFTER_PARAM, filters.windowAfter)
  if (isIsoInstant(filters.windowBefore)) params.set(BEFORE_PARAM, filters.windowBefore)
  return params
}

/**
 * Drop every owned filter, leaving the rest of the URL alone. Used on an admin switch under an open tab:
 * URL state has no per-admin bucket, so without this the incoming operator inherits the outgoing one's
 * picks. Its own verb so it keeps clearing every key even if a filter's cleared state ever emits something.
 */
export function clearTaskUrlFilters(base: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams(base)
  for (const key of OWNED_PARAMS) params.delete(key)
  return params
}

/**
 * Whether any param would need removing — for skipping a no-op `setSearchParams` on admin switch. Not
 * the badge count (that is `components/tasks/TaskToolbar.tsx`, which asks whether a date window bites
 * via `dateRangeIsActive`); this needs no calendar.
 */
export function hasTaskUrlFilters(filters: TaskUrlFilters): boolean {
  return (
    filters.q.trim() !== '' ||
    filters.showHidden ||
    filters.dateRange !== DEFAULT_TASK_DATE_RANGE ||
    filters.dateFrom !== '' ||
    filters.dateTo !== '' ||
    filters.assigneeIds.length > 0 ||
    filters.creatorIds.length > 0 ||
    filters.sources.length > 0 ||
    filters.state !== '' ||
    filters.persona !== '' ||
    filters.windowAfter !== '' ||
    filters.windowBefore !== ''
  )
}

/** Whether `state` is one the State filter may carry: a lifecycle state (`generated/taskTransitions.ts`). */
export function isTaskStateFilter(state: string): boolean {
  return (KNOWN_TASK_STATES as readonly string[]).includes(state)
}
