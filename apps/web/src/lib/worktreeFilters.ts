/* The fleet-wide `/worktrees` list's FILTER vocabulary: the dimensions, the one predicate, and the URL
 * (de)serialisation that makes any narrowed view a link. Pure of React and the API client.
 *
 * URL-OWNED, NOT STORED: `/worktrees?node=<id>` is the link the node page hands out, and a link whose
 * meaning lives in the recipient's `sessionStorage` is not a link. The query string is the source of
 * truth, so the back button works on filters.
 *
 * A NULL sensed column is "not known", never its negative (`lib/poolSlot.ts` is the single home). Reuse
 * its functions so a filter never disagrees with the cell it filters.
 *
 * A GARBAGE PARAM IS DROPPED, NEVER THROWN: a mangled link opens the unfiltered page rather than a page
 * filtered by a value nothing matches ("the fleet is empty").
 */
import { gitSummary, sessionSignal, slotIdentity, stackChipWord } from './poolSlot'
import { uniqueNonEmpty } from './strings'
import { isFolderSlot } from './worktree'
import type { NodeLabelFor } from './worktreeGrouping'
import type { Worktree } from '../types'

/** WHO HOLDS THE SLOT — `Worktree.state` (`apps/api/app/slot_state.py`), multi-select. */
export type OccupancyFilter = 'free' | 'task' | 'user'

/** IS A SESSION SITTING HERE. Single-select: both is the unfiltered list. */
export type SessionFilter = 'live' | 'none'

/**
 * THE SLOT'S STACK, three actionable states (`not-built` is `none`). An UNPROBED slot (NULL/`''`)
 * MATCHES NONE OF THE THREE: folding it into `down` or `not-built` would claim a probe that never ran.
 */
export type StackFilter = 'up' | 'down' | 'not-built'

/**
 * THE SLOT'S GIT STATE. `dirty` and `diverged` are ORTHOGONAL (a slot can be both). `clean` also requires
 * nothing UNKNOWN: a never-scanned slot is not clean (the Git column renders `—`, not `✓`).
 */
export type GitFilter = 'dirty' | 'diverged' | 'clean'

/**
 * The whole filter state, exactly what the URL carries. Multi-selects: EMPTY means no filter (no `null`,
 * no "all" member). Single-selects: `null` is the absence.
 */
export type WorktreeFilters = {
  /** Node UUIDs, never labels: a deep link must survive a rename. */
  node: readonly string[]
  project: readonly string[]
  occupancy: readonly OccupancyFilter[]
  session: SessionFilter | null
  stack: StackFilter | null
  git: GitFilter | null
  /** Free text, stored VERBATIM (a trailing space is a term in progress). WHITESPACE-ONLY IS BLANK
   *  everywhere it is asked whether this narrows (`matchesQuery`, `activeWorktreeFilterCount`,
   *  `writeWorktreeFilters`). */
  q: string
}

/** The unfiltered fleet — what a bare `/worktrees` reads as. */
export const EMPTY_WORKTREE_FILTERS: WorktreeFilters = {
  node: [], project: [], occupancy: [], session: null, stack: null, git: null, q: '',
}

const OCCUPANCY_VALUES: readonly OccupancyFilter[] = ['free', 'task', 'user']
const SESSION_VALUES: readonly SessionFilter[] = ['live', 'none']
const STACK_VALUES: readonly StackFilter[] = ['up', 'down', 'not-built']
const GIT_VALUES: readonly GitFilter[] = ['dirty', 'diverged', 'clean']

/* Membership in a closed vocabulary. Exact match, never folded: these are values this module writes. */
function pick<T extends string>(values: readonly T[], raw: string | null): T | null {
  return raw != null && (values as readonly string[]).includes(raw) ? (raw as T) : null
}

/**
 * READ the filters out of a URL. Total: every unrecognised value is dropped. Takes `URLSearchParams`, as
 * `useSearchParams` hands it.
 */
export function readWorktreeFilters(searchParams: URLSearchParams): WorktreeFilters {
  const multi = <T extends string>(key: string, values: readonly T[] | null): T[] => {
    const raw = uniqueNonEmpty(searchParams.getAll(key))
    return values == null ? (raw as T[]) : raw.filter((value): value is T => (values as readonly string[]).includes(value))
  }
  return {
    node: multi<string>('node', null),
    project: multi<string>('project', null),
    occupancy: multi('occupancy', OCCUPANCY_VALUES),
    session: pick(SESSION_VALUES, searchParams.get('session')),
    stack: pick(STACK_VALUES, searchParams.get('stack')),
    git: pick(GIT_VALUES, searchParams.get('git')),
    q: searchParams.get('q') ?? '',
  }
}

/**
 * WRITE the filters back into a URL, emitting ONLY the keys that narrow something — the badge and the
 * URL must never disagree. Multi-selects are REPEATED params (any delimiter would appear inside operator
 * data). The emission order is fixed by this body alone, so equal states produce equal strings; add a
 * key in its place below.
 *
 * `q` is emitted only when `.trim()` is non-empty (a whitespace-only `q` narrows nothing), then written
 * verbatim, spaces included.
 */
export function writeWorktreeFilters(filters: WorktreeFilters): URLSearchParams {
  const params = new URLSearchParams()
  for (const value of uniqueNonEmpty(filters.node)) params.append('node', value)
  for (const value of uniqueNonEmpty(filters.project)) params.append('project', value)
  for (const value of uniqueNonEmpty(filters.occupancy)) {
    if (pick(OCCUPANCY_VALUES, value) != null) params.append('occupancy', value)
  }
  if (filters.session != null) params.set('session', filters.session)
  if (filters.stack != null) params.set('stack', filters.stack)
  if (filters.git != null) params.set('git', filters.git)
  if (filters.q.trim() !== '') params.set('q', filters.q)
  return params
}

/* IS A SESSION SITTING HERE — read off `poolSlot.ts::sessionSignal`, what the Session CELL renders.
   `lib/worktree.ts::hasLiveSession` holds the rule (presence, never an allowlist), but `sessionSignal`
   also TRIMS, so reading it keeps filter and column agreeing on a `"  "` status. The TONE is the
   liveness bit (only `None` is `neutral`), not the label: a CLI status literally spelled `None` is live.
   Console affordances stay on `hasLiveSession` (a wire question). */
function matchesSession(slot: Worktree, want: SessionFilter): boolean {
  return (sessionSignal(slot).tone === 'info') === (want === 'live')
}

/* WHICH SLOTS EACH STACK ANSWER ADMITS — read off `poolSlot.ts::stackChipWord`, the column's own reading:
   it folds `''` into unprobed and passes unknown server values through verbatim (matching none). */
function matchesStack(slot: Worktree, want: StackFilter): boolean {
  const word = stackChipWord(slot)
  if (want === 'up') return word === 'up'
  if (want === 'down') return word === 'down'
  return word === 'none'
}

/* …AND THE GIT ANSWERS, off `poolSlot.ts::gitSummary`: `unscanned` is its own kind, so `clean` is the
   honest conjunction (scanned AND in sync AND not dirty). A folder slot has no git state, so no answer
   admits it. */
function matchesGit(slot: Worktree, want: GitFilter): boolean {
  if (isFolderSlot(slot)) return false
  const summary = gitSummary(slot)
  if (want === 'dirty') return summary.dirty === true
  if (want === 'diverged') return summary.kind === 'diverged'
  return summary.kind === 'sync' && summary.dirty === false
}

/* FREE-TEXT SEARCH over identity (`poolSlot.ts::slotIdentity`, slot 0 = `main`), Title, branch, path,
   and the node's NAME — the label is searched and the uuid is not (nobody types it), which is why the
   resolver is a parameter.
   NOT `poolSlot.ts::matchesQuery`, deliberately: that one also searches state, owner, session and model,
   right for one node's panel and noisy fleet-wide. The AND-over-terms rule is shared. */
function matchesQuery(slot: Worktree, query: string, nodeLabelFor: NodeLabelFor): boolean {
  const q = query.trim().toLowerCase()
  if (q === '') return true
  const haystack = [
    slotIdentity(slot),
    slot.title,
    slot.branch ?? '',
    slot.path,
    nodeLabelFor(slot.node_uuid),
  ]
    .join(' ')
    .toLowerCase()
  return q.split(/\s+/).every(term => haystack.includes(term))
}

/**
 * THE ONE PREDICATE — every dimension AND-ed, each multi-select OR-ed within itself, with one testable
 * answer.
 */
export function matchesWorktreeFilters(
  slot: Worktree,
  filters: WorktreeFilters,
  nodeLabelFor: NodeLabelFor,
): boolean {
  if (filters.node.length > 0 && !filters.node.includes(slot.node_uuid)) return false
  if (filters.project.length > 0 && !filters.project.includes(slot.project)) return false
  // `state` is a wire `string` (the server's vocabulary), so compare rather than cast.
  if (filters.occupancy.length > 0 && !(filters.occupancy as readonly string[]).includes(slot.state)) return false
  if (filters.session != null && !matchesSession(slot, filters.session)) return false
  if (filters.stack != null && !matchesStack(slot, filters.stack)) return false
  if (filters.git != null && !matchesGit(slot, filters.git)) return false
  return matchesQuery(slot, filters.q, nodeLabelFor)
}

/**
 * HOW MANY FILTERS ARE ON — the toolbar badge. Counts only what REMOVES ROWS (`TaskToolbar.tsx`'s rule):
 * grouping is not counted; a non-blank search is; a multi-select counts ONCE (questions, not ticks).
 */
export function activeWorktreeFilterCount(filters: WorktreeFilters): number {
  return (
    (filters.node.length > 0 ? 1 : 0) +
    (filters.project.length > 0 ? 1 : 0) +
    (filters.occupancy.length > 0 ? 1 : 0) +
    (filters.session != null ? 1 : 0) +
    (filters.stack != null ? 1 : 0) +
    (filters.git != null ? 1 : 0) +
    (filters.q.trim() !== '' ? 1 : 0)
  )
}
