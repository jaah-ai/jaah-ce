import { del, get, post } from '../../api'
import { countOf } from '../../lib/humanize'

/* Project › Memory's wire shapes and calls (`/api/projects/{id}/memory/…`). The server owns every
 * verdict (visibility, counts, feed states); this file only names and words them. */

export const MEMORY_KINDS = ['codemap', 'gotcha', 'recipe', 'env', 'qa'] as const
export const MEMORY_STATUSES = ['proposed', 'accepted', 'flagged', 'stale', 'retired', 'graduated'] as const
/* The Status filter's default (`console.STATUS_LIVE`): every status still in play, so the list opens on units that
 * can still be served, not on retired code-map chunks. */
export const STATUS_LIVE = 'live'
/* The Status filter's other extra value (`console.STATUS_HELD`): accepted units the rules-only canon check holds back
 * from serving until the memory model re-checks them. */
export const STATUS_HELD = 'held'
/* The Status filter's options, each in sentence case: Live first (the default), All, every status, then Held. */
export const MEMORY_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: STATUS_LIVE, label: 'Live' },
  { value: '', label: 'All' },
  ...MEMORY_STATUSES.map(s => ({ value: s, label: s[0].toUpperCase() + s.slice(1) })),
  { value: STATUS_HELD, label: 'Held' },
]
/* The list's sort orders (`console.LIST_SORTS`): newest first, or one of the row's counts descending. */
export const MEMORY_SORTS = [
  { value: 'newest', label: 'Newest' },
  { value: 'injected', label: 'Injected' },
  { value: 'seen', label: 'Seen' },
  { value: 'used', label: 'Used' },
  { value: 'wrong', label: 'Wrong' },
] as const
export type MemorySort = (typeof MEMORY_SORTS)[number]['value']

export type MemoryUnit = {
  id: string
  ref: string
  number: number
  kind: string
  title: string
  body: string
  status: string
  status_reason: string | null
  injectable: boolean
  trust: number
  feed: string
  paths: string[]
  /* Distinct tasks each (`console.py`'s docstring): in the prompt; delivered by the hook or the session-start slice;
   * used with evidence (a read or a settled citation); flagged wrong. */
  injected: number
  seen: number
  used: number
  wrong: number
  read: number
  cited: number
  followed: number
  /** Held by the canon check (`console.is_held`). Optional: older payloads. */
  held?: boolean
  demoted_unused: boolean
  source_task_id: number | null
  source_session_id: string | null
  source_sha: string | null
  sources: number
  created_at: string
  valid_to: string | null
  /** The unit's repository `owner/name`; set only in a project with more than one. Optional: older payloads. */
  repo?: string | null
}

export type FeedState = 'active' | 'inactive' | 'off' | 'model' | 'deterministic'

export type MemoryFeedStatus = {
  killed: boolean
  feeds: Record<'settle' | 'stop_delta' | 'codemap' | 'distill', { state: FeedState; last_at: string | null; sha?: string | null }>
  checks: {
    model_secret: { state: FeedState }
    canon: { state: FeedState; canon_sha: string | null }
  }
  codemap_sha: string | null
  /** Accepted units the canon hold keeps from runners, by feed; a feed with none is absent. Optional: older payloads. */
  held?: Partial<Record<string, number>>
}

export type UnitFilters = { q: string; kind: string; status: string; sort: MemorySort; repo?: string }

export function listUnits(projectId: number, filters: UnitFilters): Promise<MemoryUnit[]> {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.kind) params.set('kind', filters.kind)
  if (filters.status) params.set('status', filters.status)
  if (filters.sort !== 'newest') params.set('sort', filters.sort)
  if (filters.repo) params.set('repo', filters.repo)
  const query = params.toString()
  return get<MemoryUnit[]>(`/api/projects/${projectId}/memory/units${query ? `?${query}` : ''}`)
}

/* A code-map unit's directory and chunk, from the title the node writes:
 * `Code map: <dir> (<i>/<n>)`. Null for any other unit, or a directory that fit in one chunk. */
const CHUNK_TITLE = /^Code map: (.+) \((\d+)\/(\d+)\)$/

export type UnitGroup = { head: MemoryUnit; rest: MemoryUnit[] }

/** The list's rows: a directory's code-map chunks of one generation and status fold into the first one listed. */
export function groupChunks(units: MemoryUnit[]): UnitGroup[] {
  const groups: UnitGroup[] = []
  const byKey = new Map<string, UnitGroup>()
  for (const unit of units) {
    const chunk = unit.kind === 'codemap' ? CHUNK_TITLE.exec(unit.title) : null
    const key = chunk ? [chunk[1], chunk[3], unit.source_sha ?? '', unit.status].join('\u0000') : null
    const group = key === null ? undefined : byKey.get(key)
    if (group) {
      group.rest.push(unit)
      continue
    }
    const fresh = { head: unit, rest: [] }
    groups.push(fresh)
    if (key !== null) byKey.set(key, fresh)
  }
  return groups
}

/** Feed-status's cache key. Off the plan its 403 is kept for the project page's open, and dropped when it closes. */
export const memoryFeedsKey = (projectId: number) => ['project-memory-feeds', projectId] as const

export const getFeedStatus = (projectId: number) =>
  get<MemoryFeedStatus>(`/api/projects/${projectId}/memory/feed-status`)

export const retireUnit = (projectId: number, unitId: string) =>
  post<{ id: string; ref: string; status: string }>(`/api/projects/${projectId}/memory/units/${unitId}/retire`)

export const releaseUnit = (projectId: number, unitId: string) =>
  post<{ id: string; ref: string; status: string; injectable: boolean }>(
    `/api/projects/${projectId}/memory/units/${unitId}/release`)

export type PurgeOut = { id: string; ref: string; prompts_scrubbed: number }

export const purgeUnit = (projectId: number, unitId: string) =>
  del<PurgeOut>(`/api/projects/${projectId}/memory/units/${unitId}`)

/** A purge's notice: `mem-7 purged; 2 stored prompts scrubbed`. */
export const purgedNotice = (out: PurgeOut) =>
  `${out.ref} purged; ${countOf(out.prompts_scrubbed, 'stored prompt')} scrubbed`

/* One chip per feed or check: its name, its state word, and the reason when it is not running (shared by every chip
 * stopped for the same cause). A stopped chip's hover says what is off and why; the tab adds one short link to the fix, not a
 * prose line. A running Code map says which generation it serves instead of a state word: the short sha
 * in green once built, a neutral `not built` before the first build; its hover is the full sha. */
export type FeedChip = {
  key: string
  label: string
  state: string
  tone: 'success' | 'warning' | 'neutral'
  reason: string | null
  title?: string
}

export const NO_MODEL = 'No memory model credential is set'
/* Where the memory model credential is set: Settings › Runners › Project memory (`operator_settings.knobs._Section`). */
export const MEMORY_MODEL_SETTINGS = {
  href: '/settings?area=runners&section=project-memory',
  label: 'Settings › Runners › Project memory',
}
/** A check that runs its deterministic rules only, for want of the memory model. */
export const RULES_ONLY = 'Rules only'
const KILLED = 'Project memory is switched off for all runners'
/** What a model-backed feed or check stops doing without the memory model: the first half of its chip's hover. */
const NO_MODEL_EFFECT: Record<string, string> = {
  distill: 'Sessions are not distilled into units',
  model_secret: 'Units are not checked for secrets by the model',
  canon: 'Units are checked against the canon by rules only, not by the model',
}

/** `status_reason` of an accepted unit the canon hold keeps from runners (`vocabulary.CANON_CHECK_INACTIVE`). */
const CANON_CHECK_INACTIVE = 'canon check inactive'

export const isHeld = (unit: MemoryUnit) =>
  unit.held ?? (unit.status === 'accepted' && unit.status_reason === CANON_CHECK_INACTIVE)

/** `status_reason` of a gotcha proposed without a repro, kept search-only (`vocabulary.NEEDS_REPRO`). */
export const NEEDS_REPRO = 'needs repro'

/** How many units the canon check holds, across feeds; null when none is held. */
export function heldCount(status: MemoryFeedStatus): number | null {
  const count = Object.values(status.held ?? {}).reduce((sum: number, n) => sum + (n ?? 0), 0)
  return count > 0 ? count : null
}

export function feedChips(status: MemoryFeedStatus): FeedChip[] {
  const chip = (key: string, label: string, state: FeedState): FeedChip => {
    if (state === 'off') return { key, label, state: 'Off', tone: 'neutral', reason: KILLED, title: KILLED }
    const noModel = NO_MODEL_EFFECT[key] ? `${NO_MODEL_EFFECT[key]}: ${NO_MODEL.toLowerCase()}` : NO_MODEL
    if (state === 'inactive') return { key, label, state: 'Inactive', tone: 'warning', reason: NO_MODEL, title: noModel }
    if (state === 'deterministic') return { key, label, state: RULES_ONLY, tone: 'warning', reason: NO_MODEL, title: noModel }
    return { key, label, state: 'Active', tone: 'success', reason: null }
  }
  const codemap = chip('codemap', 'Code map', status.feeds.codemap.state)
  const sha = status.codemap_sha
  if (codemap.reason === null) {
    codemap.state = sha ? sha.slice(0, 7) : 'not built'
    // Nothing built is nothing served: not green until the first build lands.
    if (!sha) codemap.tone = 'neutral'
  }
  // Stopped, it still names the generation runners were last given.
  if (sha) codemap.title = codemap.title ? `${codemap.title} · built at ${sha}` : `Built at ${sha}`
  return [
    chip('settle', 'Completion', status.feeds.settle.state),
    chip('stop_delta', 'Stop delta', status.feeds.stop_delta.state),
    codemap,
    chip('distill', 'Distill', status.feeds.distill.state),
    chip('model_secret', 'Secret check', status.checks.model_secret.state),
    chip('canon', 'Canon check', status.checks.canon.state),
  ]
}
