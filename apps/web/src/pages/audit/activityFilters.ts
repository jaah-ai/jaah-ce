import { compactCount } from '../../lib/humanize'
import { isUuidShaped } from '../../lib/uuidShape'
import { localInputToIso } from '../../lib/zonedTime'
import { subjectText } from './rows'
import { Q_MAX_LEN, Q_MIN_LEN } from './queryLimits'
import type { AuditFilterLabels, AuditTotalKind } from './types'

/* AUDIT › ACTIVITY'S FILTERS, AS THE URL HOLDS THEM — pure, so the round trip is provable without a router.
 *
 *   /audit?q=&type=&user=…&org=…&result=&record=&session=&from=&to=
 *
 * Absent = default; `type` is omitted at the org's default (Write, or All for a HIPAA org). `user`/`org`
 * repeat (OR within the facet). `result` is a class (`4xx`) or an exact code (`403`); `record` is the
 * `<type>:<id>` the API's `subject` takes; `session` is one browser sign-in's id (16 hex), sent as
 * `console_session`. `from`/`to` are the datetime inputs' own wall-clock values, so a
 * pasted link means the same moment in the zone the page names. A value the API would 422 is DROPPED on
 * read, so a hand-edited link narrows by what it can and never wedges the tab. Legacy `?area=` is ignored. */

export type ActionType = 'all' | 'read' | 'write'
export type Facet = 'user' | 'org' | 'result' | 'record' | 'session'
/** A chip's facet: the URL facets, plus `q` — the committed free text, shown as its own chip. */
export type ChipFacet = Facet | 'q'

export type ActivityFilters = {
  q: string
  type: ActionType
  users: string[]
  orgs: string[]
  result: string
  record: string
  session: string
  from: string
  to: string
}

/** Each facet's chip prefix — the copy, single-homed. */
export const FACET_LABEL: Record<ChipFacet, string> = {
  q: 'Search',
  user: 'User',
  org: 'Organization',
  result: 'Result',
  record: 'Record',
  session: 'Session',
}

/* The API's own bounds: `q` ≤200 (`queryLimits.ts`), a user is a uuid or `system`, an org a uuid, a
   record `<type>:<id>` ≤200 with `type` one of `_SUBJECT_MODELS`' keys (`app/routes/audit.py`), a session
   16 lowercase hex. */
const RESULT_CLASS = /^[1-5]xx$/
const RESULT_CODE = /^[1-5]\d\d$/
const RECORD = /^[a-z_]+:[^\s:]+$/
const SESSION = /^[0-9a-f]{16}$/

export const isUserValue = (v: string) => v === 'system' || isUuidShaped(v)
export const isOrgValue = (v: string) => isUuidShaped(v)
export const isResultValue = (v: string) => RESULT_CLASS.test(v) || RESULT_CODE.test(v)
export const isSessionValue = (v: string) => SESSION.test(v)
const RECORD_TYPES: ReadonlySet<string> = new Set([
  'task', 'project', 'credential', 'node', 'account', 'user', 'group', 'cron', 'tag',
])
export const isRecordValue = (v: string) =>
  v.length <= Q_MAX_LEN && RECORD.test(v) && RECORD_TYPES.has(v.slice(0, v.indexOf(':')))

const ACTION_TYPES: readonly ActionType[] = ['all', 'read', 'write']

/** Write is the default (D8): reads are ~95% of the table and changed nothing. A HIPAA org's default is
 *  All: there a read is the PHI access an auditor looks for. */
export function defaultActionType(orgHipaa: boolean | undefined): ActionType {
  return orgHipaa ? 'all' : 'write'
}

const unique = (values: string[]) => Array.from(new Set(values))

/** The URL → filters. `operator` gates `org` (a tenant's link carrying one is read without it). */
export function readFilters(params: URLSearchParams, defaults: { type: ActionType; operator: boolean }): ActivityFilters {
  const type = params.get('type')
  const result = params.get('result') ?? ''
  const record = params.get('record') ?? ''
  const session = params.get('session') ?? ''
  const q = (params.get('q') ?? '').slice(0, Q_MAX_LEN)
  return {
    q: q.length >= Q_MIN_LEN ? q : '',
    type: ACTION_TYPES.includes(type as ActionType) ? (type as ActionType) : defaults.type,
    users: unique(params.getAll('user').filter(isUserValue)),
    orgs: defaults.operator ? unique(params.getAll('org').filter(isOrgValue)) : [],
    result: isResultValue(result) ? result : '',
    record: isRecordValue(record) ? record : '',
    session: isSessionValue(session) ? session : '',
    from: params.get('from') ?? '',
    to: params.get('to') ?? '',
  }
}

/** Filters → the URL's query, absent-means-default. Key order is fixed so one view is one address. */
export function writeFilters(filters: ActivityFilters, defaultType: ActionType): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.q) params.set('q', filters.q)
  if (filters.type !== defaultType) params.set('type', filters.type)
  for (const user of filters.users) params.append('user', user)
  for (const org of filters.orgs) params.append('org', org)
  if (filters.result) params.set('result', filters.result)
  if (filters.record) params.set('record', filters.record)
  if (filters.session) params.set('session', filters.session)
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  return params
}

/** Filters → `GET /api/audit`'s query (cleared means omitted: `?status=` is a 422). No cursor here. */
export function apiParams(filters: ActivityFilters, limit: number): URLSearchParams {
  const params = new URLSearchParams()
  params.set('action_type', filters.type)
  params.set('limit', String(limit))
  if (filters.q) params.set('q', filters.q)
  for (const user of filters.users) params.append('user_id', user)
  for (const org of filters.orgs) params.append('org_id', org)
  if (RESULT_CLASS.test(filters.result)) params.set('status_class', filters.result[0])
  else if (filters.result) params.set('status', filters.result)
  if (filters.record) params.set('subject', filters.record)
  if (filters.session) params.set('console_session', filters.session)
  const start = localInputToIso(filters.from)
  if (typeof start === 'string') params.set('start', start)
  const end = localInputToIso(filters.to)
  if (typeof end === 'string') params.set('end', end)
  return params
}

/* RESULT'S VOCABULARY, CLIENT-SIDE: the four classes, then the exact codes this console's routes return. */
export const RESULT_OPTIONS: readonly { value: string; label: string }[] = [
  { value: '2xx', label: 'Success (2xx)' },
  { value: '3xx', label: 'Redirect (3xx)' },
  { value: '4xx', label: 'Client error (4xx)' },
  { value: '5xx', label: 'Server error (5xx)' },
  { value: '200', label: '200 OK' },
  { value: '201', label: '201 Created' },
  { value: '204', label: '204 No content' },
  { value: '400', label: '400 Bad request' },
  { value: '401', label: '401 Unauthorized' },
  { value: '403', label: '403 Forbidden' },
  { value: '404', label: '404 Not found' },
  { value: '409', label: '409 Conflict' },
  { value: '422', label: '422 Unprocessable' },
  { value: '500', label: '500 Server error' },
  { value: '503', label: '503 Unavailable' },
]

/** The Result options matching typed text: by label ("forbidden", "client"), or by digits ("4" → 4xx and
 *  every 4nn). A valid code outside the shortlist is offered as itself. */
export function matchResults(text: string): { value: string; label: string }[] {
  const needle = text.trim().toLowerCase()
  if (!needle) return []
  /* Digits match the code's prefix only: "4" is not "204". */
  const numeric = /^\d+x*$/.test(needle)
  const hits = RESULT_OPTIONS.filter(o => (numeric ? o.value.startsWith(needle) : o.label.toLowerCase().includes(needle)))
  if (RESULT_CODE.test(needle) && !hits.some(o => o.value === needle)) hits.push({ value: needle, label: needle })
  return hits
}

/** A typed console-session id (16 hex, optionally `session:`-prefixed) → its Session facet value, else null: the
 *  facet is otherwise reachable only from an event's own page. */
export function matchSession(text: string): string | null {
  const value = text.trim().toLowerCase().replace(/^session:\s*/, '')
  return isSessionValue(value) ? value : null
}

export function resultLabel(value: string): string {
  return RESULT_OPTIONS.find(o => o.value === value)?.label ?? value
}

/** A record value's words: `subjectText` over the label the API resolved, else the bare reference. */
export function recordLabel(value: string, label: string | null | undefined): string {
  const at = value.indexOf(':')
  return subjectText({ type: value.slice(0, at), id: value.slice(at + 1), label: label ?? null })
}

/** A chip's label: what the pick carried (`known`), else the response's `filter_labels`, else the raw value
 *  — never blank, so a chip for a deleted user still names what it filters by. */
export function chipLabel(
  facet: ChipFacet,
  value: string,
  known: ReadonlyMap<string, string>,
  served: AuditFilterLabels | undefined,
): string {
  const picked = known.get(`${facet}:${value}`)
  if (picked) return picked
  switch (facet) {
    case 'q':
      return value
    case 'user':
      return served?.users[value] ?? (value === 'system' ? 'System' : value)
    case 'org':
      return served?.orgs[value] ?? value
    case 'result':
      return resultLabel(value)
    case 'record':
      return recordLabel(value, served?.subject)
    case 'session':
      return value
  }
}

/** The footer's count: "12 events" (exact), "About 2.3M events" (the planner's estimate, compact),
 *  "10,000+ events" (a floor). `seen` is the rows already shown across Older pages — the figure is never
 *  below it (an estimate raised to it still says "About": the rest is still a guess). */
export function eventCount(total: number, kind: AuditTotalKind, seen = 0): string {
  const shown = Math.max(total, seen)
  const noun = shown === 1 ? 'event' : 'events'
  if (kind === 'exact') return `${shown.toLocaleString('en')} ${noun}`
  if (kind === 'at_least') return `${shown.toLocaleString('en')}+ events`
  return `About ${compactCount(shown)} ${noun}`
}
