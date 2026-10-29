import { LIVE_STATES } from '../../generated/taskTransitions'
import type { DateRangeBounds } from '../../lib/taskDateRange'
import { fmtModel } from '../../lib/models'
import { ApiError, apiDetail, get } from '../../api'
import { CATALOGUE_STALE_MS, cachedRead } from '../../lib/cachedRead'
import { tabOrg } from '../../lib/activeOrg'
import { actingAs } from '../../lib/actAs'
import { humanDuration } from '../../lib/relativeTime'
import { NODE_WAITING_NOTE } from '../../lib/welcomeWizard'
import type { OrgRef, PipelineTask, TaskSourceOption, Worktree } from '../../types'
import { RunningSpinner } from '../../ui/RunningSpinner'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import { ConsoleIcon } from '../../ui/icons'
import type { PressFace } from '../../ui/pressSwap'
// The chat verdict, asked rather than re-spelled (see `isChatTask`). `taskPersona` is a type-only leaf,
// so importing it costs nothing.
import { isSystemPersona, personaLabel, rawPersonaPrefixRe } from './personaRegistry'
import { SYSTEM_PAUSE_CAUSE } from './pane/stopReason'
import { LANE_PERSONA } from './laneRun'
import { isChatTask } from './taskPersona'
// The in-flight set, generated from `apps/api/app/task_lifecycle.py` and re-exported here so `isArchiveRow`
// and the delete confirms avoid `generated/`. Deliberately not the transition table's stoppable set: spec
// R4.2 keeps them apart (one names a delete that stops a runner, the other a park).
export { LIVE_STATES }

// Pure cross-cutting helpers for the `/tasks` page, shared by more than one `components/tasks/*`
// component or the Tasks container.

// R4: the top-level list is a lazy-loaded window of TASK_PAGE_SIZE rows, grown by a page on scroll up to
// TASK_LIST_MAX, the server's ceiling (`TASK_LIST_MAX_LIMIT`, tasks.py).
// A scroll asks only the NEXT page, by the keyset cursor the previous answer carried (`?after=`, never an
// offset, so a row whose `updated_at` moved cannot shift the pages under it). Every live path refetches the
// whole window as ONE `?limit=` request — fresh rows and a fresh cursor — with the R6 generation guard for
// ordering.
export const TASK_PAGE_SIZE = 50
export const TASK_LIST_MAX = 500

/* The one "Hidden" tick → `include_hidden` + `include_archived`, shared by both routes below so
`GET /api/tasks/tag-counts` counts the same rows the list shows. Nothing written when clear, keeping the
default board's query string byte-identical to what specs assert. */
function setHiddenParams(params: URLSearchParams, showHidden: boolean) {
  if (!showHidden) return
  params.set('include_hidden', 'true')
  params.set('include_archived', 'true')
}

/* The focus toggle as one param, shared by list and counting routes. The server owns which states survive
(`_FOCUS_TOP_LEVEL_STATES`, apps/api/app/task_lifecycle.py); the browser names only the intent. Not
written when off.
── This is the single home for why focus is a request param rather than a browser filter
(`pages/Tasks.tsx` and `listViews/types.ts` point here):
· Top-level rows are a lazy, server-ordered `?limit=` window; filtering it client-side narrows the page,
not the board, and every new page arrives unfiltered.
· An expanded parent's children are the exception: they ride unbounded inside `GET /api/tasks/{id}`,
with no window and no request to carry the param, so `isFocusRow` narrows them client-side. */
function setFocusParam(params: URLSearchParams, focus: boolean) {
  if (!focus) return
  params.set('focus', 'true')
}

/* Creator picks, shared by list and counting routes so a section total matches its rows. `append`, never
`set` (several creators is normal), in pick order. Nothing written for an empty array. */
function setCreatorParams(params: URLSearchParams, creatorIds: readonly string[]) {
  for (const id of creatorIds) params.append('created_by_id', id)
}

/* Source picks, the same shape as creators: a repeated `source=` per pick (a UNION), nothing when empty. */
function setSourceParams(params: URLSearchParams, sources: readonly string[]) {
  for (const source of sources) params.append('source', source)
}

/* The search term, trimmed on the way out and omitted when empty, so the request, the toolbar badge and
`hasTaskUrlFilters` agree on whether the board is narrowed. The one normalising seam, as
`lib/taskUrlFilters.ts`'s writer is on the URL side. */
/* THE BOARD'S SCOPE, as every row-set URL below sends it: the rail scope's own narrowing params
 * (`lib/taskScope.ts::scopeToListParams` — a project's `project_id`, a feature's or "No feature"'s
 * `feature_id`); empty is All projects. One helper, so the list, its totals and its roster never disagree
 * on the set. */
export type BoardScopeFilter = URLSearchParams

function setScopeParams(params: URLSearchParams, scope: BoardScopeFilter) {
  scope.forEach((value, key) => params.append(key, value))
}

function setSearchParam(params: URLSearchParams, q: string) {
  const term = q.trim()
  if (term !== '') params.set('q', term)
}

// Build the top-level list URL (R4). `limit` is always sent ("another page?" is `rows.length >= limit`);
// the scope's params only when a scope is picked. The server's `state=` param has no caller here.
//
// `q` is sent, not filtered in the browser, because the list is a bounded window. The server matches it
// case-insensitively as one phrase against title or body (`apps/api/app/routes/tasks/listing.py::list_tasks`);
// `lib/taskUrlFilters.ts` owns the operator-facing half.
//
// `showHidden`: this function is the single home for its translation — the toolbar's one "Show hidden"
// switch becomes both `include_hidden=true` (hidden system rows) and `include_archived=true`
// (`archived_at` set). One control, by the operator's ask; asking for one class alone is not expressible.
// Both sort into the finished half (`_TASK_LIST_COMPLETED_GROUP`), so the tick never displaces live
// work, though opted-in rows interleave by recency with done rows and can push them past the window.
//
// `bounds` is the date range over last activity, already resolved by `lib/taskDateRange.ts` to at most
// two ISO instants; the vocabulary and every refusal to send a bound live there. Unlike the tick, it
// removes rows, hence it counts in the toolbar badge.
//
// `creatorIds`: "created by any of these", each a repeated `created_by_id=` (no separator escaping), in
// array order; counted in the badge. `sources` likewise, each a repeated `source=`.
export function buildTasksUrl(
  scopeFilter: BoardScopeFilter,
  showHidden = false,
  limit: number = TASK_PAGE_SIZE,
  bounds: DateRangeBounds = {},
  creatorIds: string[] = [],
  focus = false,
  /* Defaulted so five-argument callers ask for the unsearched board. */
  q = '',
  /* Newest param, defaulted like `q`: every source. */
  sources: string[] = [],
  /* The State board's window (`pages/Tasks.tsx`): `sectionOrder` sorts it as the board draws it
     (`?order=section`), so a page never fills with a later section while an earlier one waits; `excludeDone`
     drops the rows a folded Done would not draw. Neither touches the counts: Done's header keeps its total. */
  { sectionOrder = false, excludeDone = false }: { sectionOrder?: boolean; excludeDone?: boolean } = {},
  /* The State filter (`lib/taskUrlFilters.ts`), `''` for every state. */
  state = '',
): string {
  const params = boardFilterParams(scopeFilter, showHidden, bounds, creatorIds, focus, q, sources, state)
  if (excludeDone) params.set('exclude_done', 'true')
  if (sectionOrder) params.set('order', 'section')
  // After the narrowing params, `limit` being the one the counting route must not take, so this URL's
  // narrowing params are a prefix of its sibling's.
  params.set('limit', String(limit))
  // GET /api/tasks omits body/awaiting_response_body/metrics from every row (TaskListOut); expanding a row
  // refetches GET /api/tasks/{id}.
  return `/api/tasks?${params.toString()}`
}

/* The board's filter params WITHOUT `limit` — what the main pane (`./pane/`) pages with `?section=`/`?after=`
 * and asks `GET /api/tasks/counts` with, so its lists and their section counts name the SAME row set. The
 * same helpers, in the same order, as `buildTasksUrl`. */
export function boardFilterParams(
  scopeFilter: BoardScopeFilter,
  showHidden = false,
  bounds: DateRangeBounds = {},
  creatorIds: string[] = [],
  focus = false,
  q = '',
  sources: string[] = [],
  state = '',
): URLSearchParams {
  const params = new URLSearchParams()
  setScopeParams(params, scopeFilter)
  setHiddenParams(params, showHidden)
  setFocusParam(params, focus)
  setSearchParam(params, q)
  if (bounds.updatedAfter) params.set('updated_after', bounds.updatedAfter)
  if (bounds.updatedBefore) params.set('updated_before', bounds.updatedBefore)
  setCreatorParams(params, creatorIds)
  setSourceParams(params, sources)
  if (state !== '') params.set('state', state)
  return params
}

/* The section totals' URL: the same row-set question asked of the counting route, beside `buildTasksUrl`
 * so both carry the same narrowing params via the same helpers (search, hidden, creators). No `limit`:
 * the route counts the whole filtered set, since a count over the window would pose as a total.
 */
export function buildTagCountsUrl(
  scopeFilter: BoardScopeFilter,
  showHidden = false,
  bounds: DateRangeBounds = {},
  creatorIds: string[] = [],
  focus = false,
  q = '',
  sources: string[] = [],
  state = '',
): string {
  const query = boardFilterParams(scopeFilter, showHidden, bounds, creatorIds, focus, q, sources, state).toString()
  return `/api/tasks/tag-counts${query ? `?${query}` : ''}`
}

/* The creator roster's URL ("who has tasks on this board"). It carries the board scope and the hidden tick
 * (`setHiddenParams`), so the picker never offers someone whose only rows are hidden. It deliberately
 * omits:
 *   · the date window: the roster must not change under a moving pointer or make pick order matter;
 *   · creator ids: narrowing the roster by the current pick would be a one-way door.
 * Cost: a listed creator may have zero rows in the current period (`task_count` is scope-wide).
 */
export function buildTaskCreatorsUrl(scopeFilter: BoardScopeFilter, showHidden = false): string {
  const params = new URLSearchParams()
  setScopeParams(params, scopeFilter)
  setHiddenParams(params, showHidden)
  const query = params.toString()
  return `/api/tasks/creators${query ? `?${query}` : ''}`
}

/* The Source filter's vocabulary (`TaskSourceOption[]`, display order). Unscoped and fixed per deploy, so it
 * is fetched once; its labels are the server's, never spelled here. */
export const TASK_SOURCES_URL = '/api/tasks/sources'
/** The source vocabulary, fixed per deploy: one GET shared by the board's filter and the triggers' labels. */
export const readTaskSources = () => cachedRead(['task-sources'], () => get<TaskSourceOption[]>(TASK_SOURCES_URL), CATALOGUE_STALE_MS)

// Which project a new task is created in. A ladder; the first creatable rung wins ("creatable" = in
// `projects` and `canCreateIn`):
//
//   1. `boardProjectId` — the project the board is showing.
//   2. on an "All projects" board (`boardProjectId` null) only: the user's FOCUSED PROJECT
//      (`lib/focusedProject.ts`).
//   3. the first creatable project of the viewer's OWN org (`ownOrgId`), else the first creatable one in
//      roster order — NAMED BY THE CALLER, NEVER SILENT. An operator's All orgs roster leads with tenants'
//      projects, so roster order alone filed into a tenant.
//   4. `null` — nothing creatable at all, the one case a create surface must refuse rather than guess at.
//
// The caller must say where it landed whenever that is not the board: `offBoard` is
// `id !== boardProjectId`, which covers rungs 2 and 3 on an "All projects" board and a board project the
// caller may not create in. False when `id` is `null`. Callers: `useInlineTaskCreate`, `TaskEditForm`, and
// `TaskDetailPage`'s create-mode seed. Every rung asks the grant map (`useCanOnProject('tasks:create', id)`),
// so a stale id degrades down the ladder.

/** The resolved project and whether it is somewhere the operator is not looking (see above). */
export type CreateProjectChoice = { id: number | null; offBoard: boolean }

/** Just what the ladder needs; `org` lets rung 3 prefer the viewer's own org. */
export type CreatableProject = { id: number; org?: { id: string } | null }

export function resolveCreateProject(
  projects: CreatableProject[],
  boardProjectId: number | null,
  /** May this principal create in that project (`useCanOnProject('tasks:create', id)`). Defaults to
   *  allow-all for untaught hosts; every real host passes it. */
  canCreateIn: (id: number) => boolean = () => true,
  /** The user's focused project (`hooks/useFocusedProjectId`), rung 2; null when none is set. */
  focusedProjectId: number | null = null,
  /** The viewer's own org (the tab's pin, `lib/activeOrg.ts::tabOrg`), rung 3's preference; null when unknown. */
  ownOrgId: string | null = null,
): CreateProjectChoice {
  const creatable = (p: CreatableProject) => canCreateIn(p.id)
  const preferred = boardProjectId ?? focusedProjectId
  const onBoard = preferred === null ? undefined : projects.find(p => p.id === preferred && creatable(p))
  const ownOrg = ownOrgId === null ? undefined : projects.find(p => creatable(p) && p.org?.id === ownOrgId)
  const id = onBoard?.id ?? ownOrg?.id ?? projects.find(creatable)?.id ?? null
  return { id, offBoard: id !== null && id !== boardProjectId }
}

/** A create's refusal, worded. The API answers `project not found` for a project outside the org this tab
 *  acts as (an operator's "All orgs" roster lists every org's projects), so that one names the org: a member
 *  switches to it; an operator, who need not be one, reads the bare fact beside `ActAsToCreate`'s Act as. Every
 *  other refusal is the server's own sentence. */
export function createRefusal(
  err: unknown,
  fallback: string,
  project?: { org?: OrgRef | null },
  isOperator = false,
): string {
  const org = refusedInOrg(err, project)
  if (org) return isOperator ? `This project is in ${org.name}.` : `This project is in ${org.name}. Switch to ${org.name} to create tasks in it.`
  return apiDetail(err, fallback)
}

/** The org an operator must act as BEFORE creating in `project` (asked first, not after the API's 422 —
 *  `useInlineTaskCreate`): another org's project on the operator's All orgs roster. Null for everyone else, and
 *  for a project of the org this tab already acts as. */
export function actAsNeededFor(project: { org?: OrgRef | null } | undefined, isOperator: boolean): OrgRef | null {
  const org = project?.org
  const actingOrg = tabOrg()
  return isOperator && org && actingOrg && org.id !== actingOrg && actingAs()?.id !== org.id ? org : null
}

/** The org of a project a create was refused in because this tab acts as another org (`createRefusal`'s
 *  case); null for every other refusal. What `ActAsToCreate` offers to act as. */
export function refusedInOrg(err: unknown, project?: { org?: OrgRef | null }): OrgRef | null {
  const org = project?.org
  const actingOrg = tabOrg()
  return err instanceof ApiError && err.status === 422 && err.detail === 'project not found'
    && org && actingOrg && org.id !== actingOrg ? org : null
}

/** The feature a board scope names (a rail feature node), as the create ladder reads it. */
export type CreateScopeFeature = { id: string; name: string; projectId: number; status: 'active' | 'shipped' }

/** Where a new task is filed: the project ladder's answer plus the feature. `featureId` is the scope's
 *  feature when the ladder landed in ITS project and it is active; else null ("No feature" — the API refuses
 *  a mismatched pair and a shipped feature). `shippedFeature` names a SHIPPED scope feature the task
 *  therefore falls back from, so the composer can say so; null otherwise. */
export type CreateTarget = CreateProjectChoice & { featureId: string | null; shippedFeature: string | null }

export function resolveCreateTarget(
  scope: {
    projects: CreatableProject[]
    boardProjectId: number | null
    feature?: CreateScopeFeature
    focusedProjectId?: number | null
    ownOrgId?: string | null
    /** The operator's own pick on the create row; outranks the ladder while it is still creatable. */
    pickedProjectId?: number | null
  },
  canCreateIn: (id: number) => boolean = () => true,
): CreateTarget {
  const picked = scope.pickedProjectId ?? null
  const pickHolds = picked !== null && scope.projects.some(p => p.id === picked && canCreateIn(p.id))
  const choice = pickHolds
    ? { id: picked, offBoard: picked !== scope.boardProjectId }
    : resolveCreateProject(scope.projects, scope.boardProjectId, canCreateIn, scope.focusedProjectId, scope.ownOrgId ?? null)
  const feature = scope.feature !== undefined && scope.feature.projectId === choice.id ? scope.feature : undefined
  return {
    ...choice,
    featureId: feature?.status === 'active' ? feature.id : null,
    shippedFeature: feature?.status === 'shipped' ? feature.name : null,
  }
}

// The `/tasks` page surfaces the epic/`persona` pipeline. Every row carries a persona.

// ── The status vocabulary: what a row is actually doing ──
// `Task.state` has seven values, and two cover several situations: `ready` is queued, held behind an
// epic's `hold`, gated on an unmet prerequisite, or hand-Run and being claimed; `active` is launching,
// working, or (on an epic) started but still dependency-gated. So the vocabulary is STATUS, derived from
// fields the row carries (`run_requested_at`, `waiting_on_persona`, `runner_session_id`,
// `unmet_prerequisite_count`, the parent's `transition_mode` and prerequisite count): ten statuses over
// seven states, derived only in `taskStatus()`.
// Every label is one short word. `title` (tooltip and accessible name) is the word plus only the row's own
// facts — never a definition of the word, which a competent reader already knows.
export type TaskStatusKey =
  | 'draft'
  | 'queued'
  | 'waiting'
  | 'held'
  | 'starting'
  | 'running'
  | 'asking'
  | 'delivering'
  | 'failed'
  | 'paused'
  | 'done'

export type TaskStatus = {
  key: TaskStatusKey | 'unknown'
  /** The badge word. One word, always. */
  label: string
  /** Tooltip and accessible name: the word, then this row's own facts ("Waiting. Architect stage"). */
  title: string
  glyph: string
  /** The palette semantic (`ui/statusTone.ts`). */
  tone: Tone
}

// Tones: terminal-good=success, terminal-bad=error, in-flight=info, needs-you=warning, not-yet=neutral.
// Glyphs are all distinct: mobile cards show the glyph alone (asserted in shared.test.tsx).
export const TASK_STATUS: Record<TaskStatusKey, Omit<TaskStatus, 'key'>> = {
  draft: {
    label: 'Draft',
    title: 'Draft',
    glyph: '○',
    tone: 'neutral',
  },
  queued: {
    label: 'Queued',
    title: 'Queued',
    glyph: '»',
    tone: 'neutral',
  },
  waiting: {
    label: 'Waiting',
    title: 'Waiting',
    // `…` (U+2026), not `⋯` (U+22EF): the midline ellipsis is a math-font glyph, a box without one.
    glyph: '…',
    tone: 'neutral',
  },
  held: {
    label: 'Held',
    title: 'Held',
    glyph: '‖',
    tone: 'neutral',
  },
  starting: {
    label: 'Starting',
    title: 'Starting',
    glyph: '▸',
    tone: 'info',
  },
  running: {
    label: 'Running',
    title: 'Running',
    glyph: '●',
    tone: 'info',
  },
  asking: {
    label: 'Asking',
    title: 'Asking',
    glyph: '?',
    tone: 'warning',
  },
  // After the answer, before the runner has it: a warm answer leaves the row `awaiting_response` until
  // the node acks the `deliver_answer` intent. Without its own word the row would read "Asking". `info`:
  // nothing is wanted from the operator. No timeout (see `answer_delivering`).
  delivering: {
    label: 'Delivering',
    title: 'Delivering',
    glyph: '⇢',
    tone: 'info',
  },
  failed: {
    label: 'Failed',
    title: 'Failed',
    glyph: '✕',
    tone: 'error',
  },
  paused: {
    label: 'Paused',
    title: 'Paused',
    glyph: '■',
    tone: 'error',
  },
  done: {
    label: 'Done',
    title: 'Done',
    glyph: '✓',
    tone: 'success',
  },
}

// Each `state` → its status with nothing else known: the honest read for history rows (timeline,
// Activity Log). Seven entries matching `task_lifecycle.STATES` (asserted against KNOWN_TASK_STATES in
// shared.test.tsx); retired spellings fall through to RETIRED_STATE_LABELS.
export const STATUS_FOR_STATE: Record<string, TaskStatusKey> = {
  draft: 'draft',
  ready: 'queued',
  active: 'running',
  awaiting_response: 'asking',
  system_failed: 'failed',
  paused: 'paused',
  done: 'done',
}

// The state-keyed maps are projections of `TASK_STATUS`, so one edit gives a state its word, tone and
// glyph.
function statusProjection<K extends keyof Omit<TaskStatus, 'key'>>(field: K): Record<string, string> {
  return Object.fromEntries(
    Object.entries(STATUS_FOR_STATE).map(([state, key]) => [state, TASK_STATUS[key][field]]),
  )
}

/** state → the Material tone its pill reads as. */
export const STATE_TONE: Record<string, Tone> = statusProjection('tone') as Record<string, Tone>
export const STATE_GLYPH: Record<string, string> = statusProjection('glyph')
/** state → hover sentence, for list views that draw their own marks from the state-keyed maps. */
export const STATE_TITLES: Record<string, string> = statusProjection('title')

// No `stateTone(state)`/`stateGlyph(state)` accessors, deliberately: every reader uses the maps directly
// (`listViews/rows.ts` falls back to a refined glyph first) or `statusForState`/`StatusChip`. And
// `lib/poolSlot.ts` exports an unrelated `stateTone`.

// A bare `state` → its status, for the timeline and Activity Log (retired spellings via `stateLabel`'s
// RETIRED_STATE_LABELS).
export function statusForState(state: string): TaskStatus {
  const key = STATUS_FOR_STATE[state]
  if (key) return { key, ...TASK_STATUS[key] }
  const label = stateLabel(state)
  return { key: 'unknown', label, title: label, glyph: '●', tone: 'neutral' }
}

// A container (an epic, or a legacy row with children) is never dispatched; its `state` is a rollup, so
// runner-shaped refinements must not apply. `child_count` catches the legacy case.
function isContainerRow(task: PipelineTask): boolean {
  return task.persona === 'epic' || (task.child_count ?? 0) > 0
}

// Held behind the dependency gate, by its own unmet prerequisite or its parent epic's (which arrives via
// `opts`, since the child row does not carry it). Mirrors the server's dispatch gate.
function isDependencyGated(
  task: PipelineTask,
  opts?: { parentUnmetPrerequisites?: number | null },
): boolean {
  return (task.unmet_prerequisite_count ?? 0) > 0 || (opts?.parentUnmetPrerequisites ?? 0) > 0
}

/* The states in which a gate may speak for a row: `active` (work in flight, the question holds it up) and
`awaiting_response` (the row is the park). Excluded: `done`/`system_failed` (terminal; a hand-Completed
epic keeps `gated_child_id` and would read Asking), `paused` (Stop leaves child rounds open, so epic and
child would contradict), `ready` (would shadow its four more specific words), `draft` (a leftover gate).
Defence in depth against stale gate fields. */
const GATE_LIVE_STATES: readonly string[] = ['active', 'awaiting_response']

/* Does this row have its own open question? `has_open_question` covers rounds raised without parking
(leaving the row `active`); `awaiting_response` is the legacy park. Optional, degrading to the park
read. */
function hasOwnOpenGate(task: PipelineTask): boolean {
  return task.has_open_question === true || task.state === 'awaiting_response'
}

// A live row → its interim status; the single home of the refinement above.
// `parentTransitionMode` and `parentUnmetPrerequisites` are the parent epic's, which the child does not
// carry; only the epic-children tree passes them. Elsewhere a held or gated child reads "Queued", an
// honest degradation.
export function taskStatus(
  task: PipelineTask,
  opts?: { parentTransitionMode?: string | null; parentUnmetPrerequisites?: number | null },
): TaskStatus {
  // Gates first (a question outranks everything), scoped to `GATE_LIVE_STATES`.
  if (GATE_LIVE_STATES.includes(task.state)) {
    // The row's own gate first, refined to Delivering when the answer is recorded and in flight
    // (`answer_delivering_keys`, apps/api/app/task_sections.py). Server-derived, so every render point shows it
    // and it survives a reload.
    if (hasOwnOpenGate(task)) {
      /* A chat keeps `?` and the word but loses the sentence: it parks `awaiting_response` at every turn, and
      `TASK_STATUS.asking`'s sentence would point at an answer form it does not have (`answerTargetId`
      withdraws it; the API 409s). Ahead of Delivering, which is unreachable on a chat, so a stale flag cannot
      re-brand it. */
      if (isChatTask(task)) {
        return {
          key: 'asking',
          ...TASK_STATUS.asking,
          title: 'Asking. Waiting on you',
        }
      }
      if (!task.answer_delivering) return { key: 'asking', ...TASK_STATUS.asking }
      // A failing delivery says so on hover: the retry count and the node's latest reason.
      return task.answer_delivery_failure
        ? {
            key: 'delivering',
            ...TASK_STATUS.delivering,
            title: `Delivering. Retry ${task.answer_delivery_retries ?? 1}: ${task.answer_delivery_failure}`,
          }
        : { key: 'delivering', ...TASK_STATUS.delivering }
    }
    // Then a child's gate, rolled up to its container: an epic's `state` stays `active` (see
    // `gated_child_id` in types.ts), so without this the `?` never shows on the row the operator sees.
    // Below the row's own gate, so Delivering stays reachable and a parked container points at its own
    // question.
    if (task.gated_child_id != null) {
      return {
        key: 'asking',
        ...TASK_STATUS.asking,
        // Names the child: the answer screen is the subtask's.
        title: `Asking. Subtask #${task.gated_child_id}`,
      }
    }
  }
  if (task.state === 'ready') {
    // The org's hold first: nothing of this org dispatches past it, a hand Run included, so no other
    // reading of a ready row is true while it stands. The server's sentence is the tooltip.
    if (task.wait_reason) {
      // A reason that already says "Waiting…" ("Waiting for your node") is the whole title.
      const why = orgWaitLabel(task.wait_reason)
      return { key: 'waiting', ...TASK_STATUS.waiting, title: why.startsWith('Waiting') ? why : `Waiting. ${why}` }
    }
    // Ordered by which fact wins. Run bypasses the hold and dependency gates (`dispatchable_tasks`,
    // apps/api/app/routes/tasks/), so a hand-Run child really is starting.
    if (task.run_requested_at) {
      return { key: 'starting', ...TASK_STATUS.starting }
    }
    if (task.waiting_on_persona) {
      return {
        key: 'waiting',
        ...TASK_STATUS.waiting,
        title: `Waiting. ${personaLabel(task.waiting_on_persona)} stage`,
      }
    }
    // Then the dependency gate, with the shared `waiting` sentence: the blocker is another task, so
    // `waiting_on_persona` above wins when set.
    if (isDependencyGated(task, opts)) {
      // Its own prerequisites are counted in the title, so the row's hover says what it waits on.
      const unmet = task.unmet_prerequisite_count ?? 0
      return unmet > 0
        ? { key: 'waiting', ...TASK_STATUS.waiting, title: `Waiting. ${unmet} prerequisite${unmet === 1 ? '' : 's'}` }
        : { key: 'waiting', ...TASK_STATUS.waiting }
    }
    if (opts?.parentTransitionMode === 'hold') return { key: 'held', ...TASK_STATUS.held }
    // A `ready` row holding a slot (`ran_on.live`): a fresh lease its node is launching, or a slot kept from an
    // earlier attempt — either way it is not waiting for a slot, which the hover must tell apart.
    if (task.ran_on?.live) {
      return { key: 'queued', ...TASK_STATUS.queued, title: `Queued. Holds slot ${task.ran_on.slot} on ${task.ran_on.node}` }
    }
    return { key: 'queued', ...TASK_STATUS.queued, title: 'Queued. Placing it on a node' }
  }
  // `runner_session_id` is written at launch (apps/jaahd/fleet_dispatch.py), so a dispatched `active`
  // row without one is starting. Containers are excluded: never dispatched, their session is always null.
  if (task.state === 'active') {
    // A container still behind the dependency gate is waiting, not running. Container-only (a hand-Run leaf
    // has a real runner), and a live session wins (a dispatched legacy container really is working, and keeps
    // `INDICATOR_INFLIGHT`). Knowingly imprecise: an epic gated after a child was hand-Run reads Waiting while
    // that child works.
    if (!task.runner_session_id && isContainerRow(task) && isDependencyGated(task, opts)) {
      return { key: 'waiting', ...TASK_STATUS.waiting }
    }
    /* A merge-lane run is "Running" with or without a session: its run is the lane's, not a spawn the
       operator waits on, so "Starting" would never end. The tooltip says when it holds no slot, where the row
       carries `ran_on` (detail only) — the task page's Runner block reads the same fact as "no slot". */
    if (task.persona === LANE_PERSONA && !task.runner_session_id) {
      const noSlot = task.ran_on !== undefined && !task.ran_on?.live
      return { key: 'running', ...TASK_STATUS.running, title: noSlot ? 'Running. No slot' : 'Running' }
    }
    return task.runner_session_id || isContainerRow(task)
      ? { key: 'running', ...TASK_STATUS.running }
      : { key: 'starting', ...TASK_STATUS.starting }
  }
  // The bare state for rows no gate speaks for (draft, done, paused, system_failed, legacy spellings), with
  // a failed or paused row's reason after the word.
  const bare = statusForState(task.state)
  const reason = stopReasonFact(task)
  return reason ? { ...bare, title: `${bare.label}. ${reason}` } : bare
}

// A reason longer than this is cut: a tooltip shows the gist, the task page's banner the whole text.
const REASON_MAX = 240

/* Why a Failed or Paused row stopped, as the fact after its word: the failure text, who paused it, or the
system's cause. From the server's stop record (`PipelineTask.stop`), falling back to `failure_reason`. */
function stopReasonFact(task: PipelineTask): string | null {
  if (task.state !== 'system_failed' && task.state !== 'paused') return null
  const stop = task.stop
  let fact: string | null = null
  if (stop?.kind === 'failed') fact = stop.text?.trim() || null
  else if (stop?.kind === 'paused') fact = stop.by ? `By ${stop.by}` : null
  else if (stop?.kind === 'balance' || stop?.kind === 'jaah_unavailable') fact = SYSTEM_PAUSE_CAUSE[stop.kind]
  else if (stop?.kind === 'waiting_on_customer') fact = `Waiting on customer${stop.text?.trim() ? `: ${stop.text.trim()}` : ''}`
  if (fact === null && task.state === 'system_failed') fact = task.failure_reason?.trim() || null
  if (fact === null) return null
  // An epic stopped by a stage names that stage, as the pane's `stopPhrase` does.
  const stage = stop?.stage_persona ? personaLabel(stop.stage_persona) : stop?.stage_title
  if (stage) fact = `${stage}: ${fact}`
  const capped = fact.length > REASON_MAX ? `${fact.slice(0, REASON_MAX - 1)}…` : fact
  return capped.charAt(0).toUpperCase() + capped.slice(1)
}

/* Whose answer screen this row's `?` opens — one home, since the tree pill, feed rail node and release
notes all make it pressable. `null`: not asking, or asking in a way no answer screen serves (a chat). */
export function answerTargetId(task: PipelineTask): number | null {
  /* A chat has no answer screen: its `?` is a conversation awaiting the operator's next line in the Web
  Console. `/tasks/<id>?tab=answer` would render nothing, and submitting would re-dispatch a second
  runner into the chat's slot (the API 409s). Returning null leaves no `onAnswer`, so
  `taskIndicatorAction`'s console branch takes the press. */
  if (isChatTask(task)) return null
  /* Keyed on `taskStatus`, never raw state, so a press is offered exactly where `?` is drawn: Delivering
  rows have nothing to ask, and `active` + `has_open_question` rows do. */
  if (taskStatus(task).key !== 'asking') return null
  // The row's own gate first, mirroring `taskStatus`.
  return hasOwnOpenGate(task) ? task.id : (task.gated_child_id ?? null)
}

/* Where that press goes, spelled once. `?tab=answer` is the point: the detail page auto-selects Answer only
for a principal who may write it, so a read-only reader would otherwise land on the Task tab. It is read
by `hooks/useUrlTab.ts`, which falls back on a value the page does not offer. */
export function answerHref(task: PipelineTask): string | null {
  const id = answerTargetId(task)
  return id == null ? null : answerPath(id)
}

/* The path shape alone, separate from which task: `GateForm`'s `Copy all` needs its own gate's address
even mid-delivery, when `answerHref` returns null. The param stays spelled once. */
export function answerPath(id: number): string {
  return `/tasks/${id}?tab=answer`
}

// The status pill: glyph plus label, named via `title`/`aria-label`, so a hidden label still leaves a
// distinct non-colour signal. `data-status` is the machine-readable hook. Pass `taskStatus(task)` for a live
// row, `statusForState(state)` for a bare history/rollup `state`.
export function TaskStatusBadge({ status }: { status: TaskStatus }) {
  return (
    <StatusChip
      label={status.label}
      tone={status.tone}
      glyph={status.glyph}
      title={status.title}
      glyphTestId="badge-glyph"
      labelTestId="badge-label"
      data-status={status.key}
    />
  )
}

/* The wait reason as the task page's banner and footer read it: the server's sentence without the badge's own word
 * ("Waiting: organization runner limit (1/1)" → "Organization runner limit (1/1)"). A capacity hold (no remedy of
 * its own, so no closing ".") and the node wait say when they clear. The list chip (`orgWaitLabel`) is this sentence's head. */
export function orgWaitSentence(reason: string): string {
  // The node wait says when it ends too: it does not start until a node is online.
  if (reason === NODE_WAITING_NOTE) return `${reason} — starts once your node is online.`
  const rest = reason.replace(/^Waiting:\s*/, '')
  const sentence = rest.charAt(0).toUpperCase() + rest.slice(1)
  return reason.startsWith('Waiting:') && !reason.endsWith('.') ? `${sentence} — starts when room frees up.` : sentence
}

/* The wait reason as a list or pill label: the banner's wording, first clause only. */
export function orgWaitLabel(reason: string): string {
  // The first sentence only: a reason carrying its own remedy (`app/dispatch_wait.py::held_on_offline_node`)
  // keeps the label short; the full sentence rides the pill's `title` and the task page's banner.
  return orgWaitSentence(reason).split(' — ')[0].split('. ')[0].replace(/\.$/, '')
}

/* The wait reason as the task page header's one pill: the badge's word and the reason as one fact —
 * "Waiting for your node", or "Waiting · Organization runner limit (1/1)" where the reason does not start with it. */
export function orgWaitPillLabel(reason: string): string {
  const label = orgWaitLabel(reason)
  return label.startsWith(TASK_STATUS.waiting.label) ? label : `${TASK_STATUS.waiting.label} · ${label}`
}

// ── The status indicator: the pill an operator can press ──
// Same pill, word and tone; on three statuses where status and next action coincide, it carries the
// action:
//
//   in flight (running/starting) → an animated spinner; pressing it opens the runner's web console
//   draft / paused              → its own glyph (○/■); pressing it starts the row
//   done                        → the resting ✓ every Done surface shows, and nothing to press
//
// …and on any other status, the console, where the host passes a slot: the task page does for any slot its
// task holds; the board only for a live row. Draft and paused press to Start whenever the host
// passes Start.
//
// This is the phone card's one-tap path (Start/Stop/Web Console sit behind ⋯), at every width. The
// spinner is the indicator's own, kept out of `TASK_STATUS` (history rows have nothing to spin); every
// other pill keeps its state's glyph, so a state reads the same on the board, the header and the Timeline.
const INDICATOR_INFLIGHT: readonly TaskStatus['key'][] = ['running', 'starting']

// The interactive pill's accessible name: `<Status> — <what pressing it does>: <the row>`.
// The colon is a constraint: this pill shares a row with named buttons (`Start <row>`, `Open web console
// for <row>`), and accessible-name queries match by substring (Playwright, Testing Library), so a name
// containing "start <row>" would make those queries ambiguous (three e2e specs failed on it).
export function indicatorName(statusLabel: string, action: string, name: string): string {
  return `${statusLabel} — ${action}: ${name}`
}

/* Which verb the indicator carries and how it names itself — hoisted out of `TaskStatusIndicator` so the
Activity feed's rail node (`listViews/ActivityFeedView.tsx`) offers the same press without a second copy
of the rule. */
export type TaskIndicatorAction = {
  kind: 'console' | 'start' | 'answer' | 'none'
  /** Absent exactly when `kind` is `'none'`. */
  onPress?: () => void
  /** The hover sentence and accessible name; both name the press when there is one. */
  title: string
  ariaLabel: string
  /** The bare press phrase ("press to start"), or `null`. For a surface with its own state words (the
   *  feed's rail node, via `listViews/rows.ts`), which composes its own text from this and `indicatorName`.
   *  The pill uses `title`/`ariaLabel`. */
  verb: string | null
}

// What a hover says a click does, named once for the pill and the feed's rail node (`listViews/feedRow.tsx`).
export const INDICATOR_HOVER: Record<'console' | 'start' | 'answer', string> = {
  console: 'Click to open Console',
  start: 'Click to start',
  answer: 'Click to answer',
}

/* What a pressable indicator turns into while pointed at or focused (`ui/pressSwap.tsx`): the
action's mark and word — the task page's console icon, ▶ Start, ✎ Answer. A function, not a map: `ACTION_ICON`
is declared further down. */
export function indicatorPress(kind: TaskIndicatorAction['kind']): Required<PressFace> | undefined {
  switch (kind) {
    case 'console': return { glyph: <ConsoleIcon />, label: CONSOLE_WORD }
    case 'start': return { glyph: ACTION_ICON.start, label: TASK_ACTIONS.start.verb }
    case 'answer': return { glyph: ACTION_ICON.answer, label: ANSWER_WORD }
    case 'none': return undefined
  }
}

/* The hover: the status (word plus the row's facts), where the runner works when the press opens its
console, then what the click does — `Running. jaah-2/Opus 4.8/Main. Click to open Console`. */
export function indicatorHover(statusTitle: string, kind: TaskIndicatorAction['kind'], consoleSlot?: Worktree): string {
  if (kind === 'none') return statusTitle
  const where = kind === 'console' && consoleSlot ? slotWhere(consoleSlot) : ''
  return [statusTitle, where, INDICATOR_HOVER[kind]].filter(Boolean).join('. ')
}

// The press phrases, named once: each is spoken into two names.
const CONSOLE_VERB = 'press to open the web console'
const START_VERB = 'press to start'
// "press to answer", not "answer": with `indicatorName`'s colon it stays distinct as a substring from the
// detail page's `Answer` tab, `Submit answer` button and scoped `getByLabel('Answer')` (audited against
// the end-to-end suite).
const ANSWER_VERB = 'press to answer'

export function taskIndicatorAction(
  status: TaskStatus,
  /** The row's own name; the indicator is named for what it does. */
  name: string,
  handlers: { onStart?: () => void; onOpenConsole?: () => void; onAnswer?: () => void },
  /** The runner slot the console press opens: its node, model and account name the hover. */
  consoleSlot?: Worktree,
): TaskIndicatorAction {
  const { onStart, onOpenConsole, onAnswer } = handlers
  /* The `?` is pressable, first: a row that wants an answer wants nothing else. Keyed on
  `status.key === 'asking'`, not raw state, so the press is offered exactly when `?` shows: Delivering
  has no answer to give (it opens the console below), and a container asking via the child-gate rollup
  points (via `answerHref`) at the child. */
  if (status.key === 'asking' && onAnswer !== undefined) {
    return {
      kind: 'answer',
      onPress: onAnswer,
      title: indicatorHover(status.title, 'answer'),
      ariaLabel: indicatorName(status.label, ANSWER_VERB, name),
      verb: ANSWER_VERB,
    }
  }
  /* Then Start, on a draft or paused row: pressing it starts the row on every surface. Ahead of the console: a
  paused task still holds its slot, and a press that opened the console there instead recorded no Start, so the
  task kept pushing as whoever started it last.
  The console stays one item away in the ⋯ menu. */
  if ((status.key === 'draft' || status.key === 'paused') && onStart !== undefined) {
    return {
      kind: 'start',
      onPress: onStart,
      title: indicatorHover(status.title, 'start'),
      ariaLabel: indicatorName(status.label, START_VERB, name),
      verb: START_VERB,
    }
  }
  /* Then the console, at ANY other status: the host passes `onOpenConsole` only where a runner slot
  resolved, so the handler is the whole test. The board's hosts resolve one only for a live row
  (`rowActions.tsx` `consoleSlotFor`); the task page for any slot the task holds, so its Asking,
  Queued or Done pill opens the console too. An asking row lands here when no answer hop was passed: a chat
  (`answerTargetId` returns null; its screen is the console), or the task page, which IS the answer
  screen. */
  if (onOpenConsole !== undefined) {
    return {
      kind: 'console',
      onPress: onOpenConsole,
      title: indicatorHover(status.title, 'console', consoleSlot),
      ariaLabel: indicatorName(status.label, CONSOLE_VERB, name),
      verb: CONSOLE_VERB,
    }
  }
  // Everything else states its status and offers nothing.
  return { kind: 'none', title: status.title, ariaLabel: status.title, verb: null }
}

/* Where a runner works, for the console hover: `jaah-2/Opus 4.8/Main` — node, model, account. Parts the
slot does not carry are dropped; the account falls back to its slug (the config dir's basename). */
export function slotWhere(slot: Worktree): string {
  const account = slot.account_name || (slot.account ? slot.account.split('/').filter(Boolean).pop() : null)
  return [slot.node_id, slot.model ? fmtModel(slot.model) : null, account].filter(Boolean).join('/')
}

// `ui/StatusChip`'s sanctioned className hook on the pressable pill, for a host that styles it.
export const INDICATOR_ACTION_CLASS = 'status-indicator-action'

export function TaskStatusIndicator({
  task,
  name,
  parentTransitionMode,
  parentUnmetPrerequisites,
  onStart,
  onOpenConsole,
  consoleSlot,
  onAnswer,
  spellWaitReason,
}: {
  task: PipelineTask
  /** The row's own name; the interactive pill is named for what it does. */
  name: string
  /** A waiting row's pill reads its org hold whole ("Waiting for your node"), not the bare word: the task page's
   *  header, where it has the room. */
  spellWaitReason?: boolean
  parentTransitionMode?: string | null
  parentUnmetPrerequisites?: number | null
  /** Start this row. Omitted → a draft/paused pill stays inert. */
  onStart?: () => void
  /** Open this row's runner console. Omitted when no console slot resolved. */
  onOpenConsole?: () => void
  /** The slot `onOpenConsole` opens, so the hover names its node, model and account. */
  consoleSlot?: Worktree
  /** Go answer the question this row shows — its own or the rolled-up subtask's (`answerHref` owns which).
   *  Omitted → an asking pill stays inert. */
  onAnswer?: () => void
}) {
  const bare = taskStatus(task, { parentTransitionMode, parentUnmetPrerequisites })
  const status = spellWaitReason && bare.key === 'waiting' && task.state === 'ready' && task.wait_reason
    ? { ...bare, label: orgWaitPillLabel(task.wait_reason), title: orgWaitSentence(task.wait_reason) }
    : bare
  // Which verb and its name come from `taskIndicatorAction`; the pill draws only the glyph and chip.
  const action = taskIndicatorAction(status, name, { onStart, onOpenConsole, onAnswer }, consoleSlot)

  if (INDICATOR_INFLIGHT.includes(status.key)) {
    return (
      <StatusChip
        label={status.label}
        tone={status.tone}
        glyph={<RunningSpinner />}
        title={action.title}
        ariaLabel={action.ariaLabel}
        onClick={action.onPress}
        press={indicatorPress(action.kind)}
        className={action.kind === 'console' ? INDICATOR_ACTION_CLASS : undefined}
        glyphTestId="badge-glyph"
        labelTestId="badge-label"
        data-status={status.key}
      />
    )
  }
  /* The `?` pill (and Delivering's ⇢), pressable, with the status's own glyph (the `?` already is the verb).
     `INDICATOR_ACTION_CLASS` carries the 44px phone floor. Keyed on "asking and there is a press", not
     `kind === 'answer'`: a chat's `?` presses to the console and would otherwise fall to the inert badge.
     Likewise any other status whose host resolved a runner slot (the task page), or a draft/paused row's Start: its
     own glyph, pressable (never a ▶ that reads "▶ Draft" beside the Timeline's "○ Draft"). */
  if (
    ((status.key === 'asking' || status.key === 'delivering') && action.onPress !== undefined)
    || action.kind === 'console'
    || action.kind === 'start'
  ) {
    return (
      <StatusChip
        label={status.label}
        tone={status.tone}
        glyph={status.glyph}
        title={action.title}
        ariaLabel={action.ariaLabel}
        onClick={action.onPress}
        press={indicatorPress(action.kind)}
        className={INDICATOR_ACTION_CLASS}
        glyphTestId="badge-glyph"
        labelTestId="badge-label"
        data-status={status.key}
      />
    )
  }
  // Done included: one glyph per state everywhere, so the header pill matches the Details State field.
  return <TaskStatusBadge status={status} />
}

// A cached expanded-detail entry is either the fetched Task or a per-row error marker.
export type ExpandedEntry = PipelineTask | { error: string }

// R4.4: the web mirror of `_EPIC_SETTLED_STATES` (`apps/api/app/task_lifecycle.py`). Two members: a
// `paused` child is stopped, not finished, so its phase stays expanded (R4.1).
// Not derivable from the transition table: "may take `start`" and "the work is over" differ (as with
// `LIVE_STATES`, spec R4.2).
export const EPIC_SETTLED_STATES = new Set(['done', 'system_failed'])

// R2/R3: a task the pipeline has not finished with. Drives per-phase completion and collapse-by-default;
// a `paused` child keeps its phase open (plan D5).
export function isActive(t: PipelineTask): boolean {
  return !EPIC_SETTLED_STATES.has(t.state)
}

/* The focus toggle's child state set, mirroring `_FOCUS_STATES` (apps/api/app/task_lifecycle.py), the
authority. A mirror exists because expanded children arrive unbounded inside `GET /api/tasks/{id}`
(see `setFocusParam`); top-level rows are filtered server-side.
Narrower than the server's top-level set on purpose: `_FOCUS_TOP_LEVEL_STATES` keeps `ready` (its block
says why), while a queued child should not reappear. Five members, not `!EPIC_SETTLED_STATES.has(state)`,
which would let `ready` through. */
const FOCUS_STATES = new Set(['draft', 'active', 'awaiting_response', 'paused', 'system_failed'])

/* Would this child row survive focus? Children only (see above). The archived test mirrors the server
predicate's second half (`visible_epic_children` already excludes archived children), since focus
overruling `include_archived` is the contract. `!= null` catches both spellings. */
export function isFocusRow(t: PipelineTask): boolean {
  return FOCUS_STATES.has(t.state) && t.archived_at == null
}

// A compact "how long active" label for an `active` child. There is no `started_at` column; fetching
// `GET /tasks/{id}/activity` per row would be N+1 per render, so `updated_at` is the documented fallback.
// Clamped at zero; `wholeMinutes` floors to minutes and keeps the zero (`<1m`, never a `0m` that reads as no time).
export function elapsedLabel(updatedAt: string): string {
  return humanDuration((Date.now() - new Date(updatedAt).getTime()) / 1000, true)
}

/** How long a runner may take to announce its session before "starting…" stops being believable. */
export const SPAWN_WINDOW_MS = 5 * 60_000

// Has a session-less run outlived the spawn window (from the same `updated_at` as `elapsedLabel`)?
export function pastSpawnWindow(updatedAt: string): boolean {
  return Date.now() - new Date(updatedAt).getTime() > SPAWN_WINDOW_MS
}

// `compactAge` lives in `lib/relativeTime.ts`. `elapsedLabel` stays: a two-unit duration for one
// in-flight task, not an age.

// Whether a row shows only because "Hidden" is ticked. Reads `hidden` first (the sole UI-filter
// authority, P3b), falling back to persona membership for payloads that omit it; `hidden` arrives with
// the row, so the pre-roster fallback costs only a marker on such payloads.
// The marker means "kept off the board", not "the system wrote this": a Runner cron's `hidden` tick
// (`app/cron_activity_runner.py`) files the operator's own maintenance as a `hidden` `coder` row, so the
// rendered copy must not say "System task".
// A row asking a question is on the default board whatever `hidden` says (`authz.unhidden_task_clause`), so
// it carries no marker.
export function isHiddenSystemRow(task: { hidden?: boolean; persona: string; state?: string }): boolean {
  if (task.state === 'awaiting_response') return false
  return task.hidden === true || isSystemPersona(task.persona)
}

/** The ⚙ marker's hover sentence for an `isHiddenSystemRow` row. */
export const SYSTEM_ROW_TITLE = 'Kept off the board — system bookkeeping, or flagged hidden'

// Which half of the board a top-level row belongs to (`true` = finished).
// The client half of a hand-mirrored pair: the server's `_TASK_LIST_COMPLETED_GROUP`
// (apps/api/app/routes/tasks/) orders the response; this places the separator. Terms are spelled the
// same, in the same order, and tested on each side (shared.test.tsx, test_tasks_routes_listing.py). Not generated
// (unlike `generated/taskTransitions.ts`): three predicates over two columns and one state.
// `hidden === true`, not `isHiddenSystemRow`: the server reads the column alone (P3b), and the persona
// fallback would misplace a `hidden=false` system-persona row. A hidden row asking a question is unfinished.
export function isCompletedRow(task: PipelineTask): boolean {
  return (
    task.state === 'done' ||
    task.archived_at != null ||
    (task.hidden === true && task.state !== 'awaiting_response')
  )
}

// `_create_epic_with_children` (apps/api routes/tasks/create.py) stores analysis children as `[<persona>]
// <title>`; this strips that prefix at display, from the served roster's slugs (`rawPersonaPrefixRe`,
// rebuilt with the roster; matches nothing before it lands). `showPersona: true` only where nothing else
// names the persona (AC2.3).

// Legacy titles can carry literal backslash-escapes (`\[ctrl-256\]`). Titles render as plain text, so this
// mirrors CommonMark's backslash-escapable ASCII set rather than pulling in a parser.
const MARKDOWN_ESCAPABLE_PUNCTUATION = new Set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'.split(''))

export function deescapeMarkdownText(text: string): string {
  let out = ''
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\\' && i + 1 < text.length && MARKDOWN_ESCAPABLE_PUNCTUATION.has(text[i + 1])) {
      out += text[i + 1]
      i++
    } else {
      out += text[i]
    }
  }
  return out
}

export function displayTitle(title: string, opts?: { showPersona?: boolean }): string {
  const match = title.match(rawPersonaPrefixRe())
  if (!match) return deescapeMarkdownText(title)
  const rest = deescapeMarkdownText(title.slice(match[0].length))
  return opts?.showPersona ? `${personaLabel(match[1])} — ${rest}` : rest
}

// `state` → a plain one-word label, a projection of TASK_STATUS so state and status words never
// disagree. Every state render point reads through it; unmapped values render de-underscored, never as
// a raw slug. The meaning lives in each status's `title`.
export const STATE_LABELS: Record<string, string> = statusProjection('label')

// Retired states: never a live `state`, but still rendered (via `statusForState` → `TaskStatusBadge`) in TaskDetailPage's timeline
// and Activity Log from pre-cutover `activity_events`. Retired stage states map to the same
// Analyze/Design/Plan words as their persona successors, so history never shows a raw slug.
export const RETIRED_STATE_LABELS: Record<string, string> = {
  business_analysis: 'Analyze',
  architecture: 'Design',
  decomposing: 'Planning',
  decomposed: 'Planned',
  pending_review: 'In review',
  // The migration-0082 rename: live rows read `awaiting_response`/`system_failed`/`paused` while history
  // holds these spellings, so both read off TASK_STATUS and render the same words.
  blocked: TASK_STATUS.asking.label,
  failed: TASK_STATUS.failed.label,
  triaged: TASK_STATUS.paused.label,
  // R1's retired transient; `task_lifecycle.STATES` dropped it, but history still renders it.
  marked_for_dev: 'Queued',
}

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? RETIRED_STATE_LABELS[state] ?? state.replace(/_/g, ' ')
}

// Epic-detail child grouping: children group by persona into fixed sections (except a
// Finish-filed follow-up — see `personaGroupOf`); unknown personas fall into "Other". Array order is
// render order (Prepare → Build → Finish → Other); empty groups are omitted.
// The three analysis stages share one "Prepare" section, by the operator's ask, reversing an earlier split (which has
// no ADR — this comment is the single home of the reversal). Display only: `task_dependencies` still
// sequences them, and no `phase_index`/`phase_name` is stamped (see `append_tasks`).
// The one persona list in `apps/web` not derived from the served roster: no served field says which
// section a persona renders in, and `mode === 'analysis' && delivery === 'db'` would also match `finish`
// and `prototyper` (persona_registry.py). shared.test.tsx pins each name as a registered pipeline persona.
export const PREPARE_PERSONAS: readonly string[] = ['business_analysis', 'architecture', 'plan']

export type PersonaGroupKey = 'prepare' | 'coder' | 'finish' | 'other'

// `finish` after `coder`. The group key is the persona (`coder`); the label stays
// "Build", the phase an operator sees.
export const PERSONA_GROUPS: { key: PersonaGroupKey; label: string }[] = [
  { key: 'prepare', label: 'Prepare' },
  { key: 'coder', label: 'Build' },
  { key: 'finish', label: 'Finish' },
  { key: 'other', label: 'Other' },
]

// The persona→section map; its literals are the same presentation choice as PREPARE_PERSONAS
// (`prototyper` shares `finish`'s pair yet belongs in Build), pinned by the same test.
export function personaGroupOf(t: PipelineTask): PersonaGroupKey {
  if (PREPARE_PERSONAS.includes(t.persona)) return 'prepare'
  // A Finish-filed follow-up (a `coder` row with `finish_follow_up_key`) lands in Finish: Build had already
  // completed, and it carries no `phase_index` for `coderPhaseGroups`. The analysis check stays first.
  if (t.finish_follow_up_key) return 'finish'
  // `translate` and `marketer` are build children a plan files beside `coder` (their phases group with
  // theirs); `prototyper` sits in Build too: all are where something gets made (`prototyper` only
  // reachable if it becomes an epic child). `researcher` and `onboard` fall to `other`: top-level only, and not a build.
  if (t.persona === 'coder' || t.persona === 'translate' || t.persona === 'marketer'
    || t.persona === 'prototyper') return 'coder'
  if (t.persona === 'finish') return 'finish'
  return 'other'
}

// The Prepare subtask titles `Analyze: {epic_title}` / `Design: …` / `Plan: …` — the one home of that
// format, derived at display from the epic's current title (survives renames and legacy stored titles).
// Needed because the merged "Prepare" header no longer names the persona. Stage words via `personaLabel`,
// the title via `displayTitle`.
export function prepareChildTitle(child: PipelineTask, epicTitle: string): string {
  return PREPARE_PERSONAS.includes(child.persona) ? prepareStageTitle(child.persona, epicTitle) : displayTitle(child.title)
}

function prepareStageTitle(persona: string, epicTitle: string): string {
  return `${personaLabel(persona)}: ${displayTitle(epicTitle)}`
}

/** A row's title read on its OWN page (the crumb), where no epic title is at hand: a Prepare stage's stored
 *  `[<persona>] <epic title>` reads as the board row does (`prepareChildTitle`), any other prefix is stripped. */
export function storedTitleAsShown(title: string): string {
  const match = title.match(rawPersonaPrefixRe())
  return match && PREPARE_PERSONAS.includes(match[1])
    ? prepareStageTitle(match[1], title.slice(match[0].length))
    : displayTitle(title)
}

// A rendered child section: a fixed PERSONA_GROUPS bucket or one Phase-DAG sub-group of Build.
export type ChildGroup = { key: string; label: string; tasks: PipelineTask[] }

// Phase-DAG: coders group into planner-named phases (`phase_index`/`phase_name`); phase k+1
// depends on all of phase k (`append_tasks`). Legacy coders go to a trailing "Unphased" group.
function coderPhaseGroups(tasks: PipelineTask[]): ChildGroup[] {
  const byPhase = new Map<number, PipelineTask[]>()
  const unphased: PipelineTask[] = []
  for (const t of tasks) {
    if (t.phase_index != null) {
      const bucket = byPhase.get(t.phase_index) ?? []
      bucket.push(t)
      byPhase.set(t.phase_index, bucket)
    } else {
      unphased.push(t)
    }
  }
  const groups: ChildGroup[] = Array.from(byPhase.keys())
    .sort((a, b) => a - b)
    .map(phaseIndex => {
      const phaseTasks = byPhase.get(phaseIndex)!
      const name = phaseTasks.find(t => t.phase_name)?.phase_name
      return {
        key: `coder-phase-${phaseIndex}`,
        label: name ? `Phase ${phaseIndex} — ${name}` : `Phase ${phaseIndex}`,
        tasks: phaseTasks,
      }
    })
  if (unphased.length > 0) {
    groups.push({ key: 'coder-unphased', label: 'Unphased', tasks: unphased })
  }
  return groups
}

// An epic's ordered child sections (`SubtaskList.tsx`'s phases): Prepare / Finish / Other buckets plus Build expanded into its
// Phase-DAG sub-groups; every child in exactly one. Build is what `personaGroupOf` routes there, not every
// `coder` child. Empty buckets omitted; array order is render order.
export function epicChildGroups(children: PipelineTask[]): ChildGroup[] {
  const groups: ChildGroup[] = []
  for (const g of PERSONA_GROUPS) {
    const tasks = children.filter(c => personaGroupOf(c) === g.key)
    if (tasks.length === 0) continue
    if (g.key === 'coder') {
      groups.push(...coderPhaseGroups(tasks))
    } else if (g.key === 'prepare') {
      // Analyze → Design → Plan deterministically: payload order (`order_by(Task.id)`) is an accident. Sorts
      // the filtered copy only.
      groups.push({
        key: g.key,
        label: g.label,
        tasks: [...tasks].sort(
          (a, b) => PREPARE_PERSONAS.indexOf(a.persona) - PREPARE_PERSONAS.indexOf(b.persona),
        ),
      })
    } else {
      groups.push({ key: g.key, label: g.label, tasks })
    }
  }
  return groups
}

// The single home for every confirm-guarded action's verb (trigger, dialog title, submit).
// `busy` is the in-flight label; `bulk` derives from the same singular verb.
export const TASK_ACTIONS = {
  build: { verb: 'Send to build', busy: 'Sending…', bulk: (n: number) => `Send ${n} to build` },
  archive: { verb: 'Archive', busy: 'Archiving…', bulk: (n: number) => `Archive ${n}` },
  delete: { verb: 'Delete', busy: 'Deleting…', bulk: (n: number) => `Delete ${n}` },
  continue: { verb: 'Continue', busy: 'Continuing…' },
  // The unified epic Start (any non-active → active), paired with Stop.
  start: { verb: 'Start', busy: 'Starting…' },
  stop: { verb: 'Stop', busy: 'Stopping…' },
  reset: { verb: 'Reset', busy: 'Resetting…' },
} as const

// The one wording for "Reset destroys the attempt", shared by the board's Reset (Tasks.tsx) and the edit
// form's Go on an `active` row (`goTransitions`), whose intent reads as "restart with my edits".
// `epic`: an epic's Reset cascades to every subtask (`routes/tasks/transitions.py::reset_task`), so the dialog says so.
export function resetConfirmOptions(title: string, opts?: { thenRestart?: boolean; epic?: boolean }) {
  return {
    title: opts?.thenRestart ? 'Save and restart' : TASK_ACTIONS.reset.verb,
    message: opts?.epic ? (
      <>
        Reset “<strong>{title}</strong>” and all its subtasks to clean drafts? Their questions and
        answers, reports, stage output and outcomes stop being shown, live runners are killed and
        slots released. Descriptions and attachments are kept; Start runs the epic from the top.
      </>
    ) : (
      <>
        {opts?.thenRestart ? 'Save and restart' : 'Reset'} “<strong>{title}</strong>”
        {opts?.thenRestart ? '?' : ' to a clean draft?'} Its questions and answers, report, stage output
        and outcome stop being shown, and a live runner is killed. The task’s own description and
        attachments are kept.
      </>
    ),
    confirmLabel: opts?.thenRestart ? 'Save and restart' : TASK_ACTIONS.reset.verb,
    destructive: true,
  }
}

// The one wording for Stop, shared by the board's Stop (Tasks.tsx) and the task page's ⋯. `queued`: a
// row Stop accepts that can ALSO still Start had nothing dispatched against it, so no runner is promised.
export function stopConfirmOptions(title: string, queued: boolean) {
  return {
    title: 'Stop task',
    message: (
      <>
        Stop “<strong>{title}</strong>”? This parks it as <strong>paused</strong> (you can
        continue it later).
        {queued ? '' : ' A live runner is not force-killed — it reconciles on its own.'}
      </>
    ),
    confirmLabel: TASK_ACTIONS.stop.verb,
    destructive: true,
  }
}

/* What this press destroys, as a discriminated union: a nullable title meant "create" and made the
 * half-landed create (row filed, attachment failed) say nothing was created. A union cannot be
 * under-answered.
 */
export type DiscardSubject =
  /** A task that does not exist yet: the whole draft goes and nothing is left behind. */
  | { kind: 'create' }
  /** A create whose POST LANDED and whose follow-on step did not: the row survives, the step does not. */
  | { kind: 'created'; title: string }

/* The wording for "Discard throws away what you typed": irreversible (`TaskEditForm`'s
 * `discardAndCancel`) and one button from Save. Named here, as `resetConfirmOptions`, so it states what is
 * lost consistently; kept separate because Reset destroys a stored attempt, this an unsaved edit.
 * Raised only by the create surface: editing auto-saves and shows `Close` (`TaskEditForm`'s
 * `offersDiscard`).
 */
export function discardConfirmOptions(subject: DiscardSubject) {
  /* An empty Title has no name to quote; `undefined` explicitly rather than relying on `ConfirmDialog`'s
  truthiness. */
  const named = subject.kind === 'create' ? '' : displayTitle(subject.title).trim()
  return {
    title: 'Discard changes',
    /* The row in the identity block, not mid-prose, where a long title pushed the "what is lost" clause down.
    Clamped: it only has to identify the row. */
    identity: named === '' ? undefined : clampForConfirm(named),
    message: subject.kind === 'create' ? (
      <>
        Discard this new task? What you have typed here is thrown away and nothing is created.
      </>
    ) : (
      /* A row an auto-save already filed stays (the operator's ruling), so this must not promise un-filing. */
      <>
        This task is already created and keeps everything that was saved. Discarding abandons only the
        step that has not finished yet; the task stays on the list.
      </>
    ),
    confirmLabel: 'Discard',
    /* Not 'Cancel': the confirm is raised by a former Cancel button. Not "Keep editing" on the half-landed
    create, where the editor is locked (`data-locked`). */
    cancelLabel: subject.kind === 'created' ? 'Stay here' : 'Keep editing',
    destructive: true,
  }
}

/* A confirm banner's title budget: 72 characters, a bit over one line of the 420px modal at 0.8125rem.
Trailing space trimmed before the ellipsis. Exported so the test pins this number. */
export const CONFIRM_TITLE_CHARS = 72

function clampForConfirm(text: string): string {
  /* Code points, never UTF-16 units: an emoji's surrogate pair cut in half renders a replacement glyph. */
  const points = Array.from(text)
  return points.length <= CONFIRM_TITLE_CHARS
    ? text
    : `${points.slice(0, CONFIRM_TITLE_CHARS - 1).join('').trimEnd()}…`
}

// The two verbs with no `TASK_ACTIONS` entry (neither is confirm-guarded) that more than one surface speaks:
// the ⋯ item and Answer control (`rowActions.tsx`, `controls.ts`) and the pressed pill (`indicatorPress`).
export const CONSOLE_WORD = 'Console'
export const ANSWER_WORD = 'Answer'

// The row-action glyphs, single-homed. Unicode, no icon library; decorative (`aria-hidden`), each button
// keeps its own aria-label.
export const ACTION_ICON = {
  edit: '✎',
  // The question's Answer (`controls.ts`, and the asking pill's press, `indicatorPress`); the same mark as Edit.
  answer: '✎',
  start: '▶',
  // "Run now" (an automation's one-off firing), told apart from Resume's ▶ in the same menu; text presentation.
  run: '⚡\uFE0E',
  stop: '⏹',
  // What went wrong: a failing row's "View failure"; text presentation.
  failure: '⚠\uFE0E',
  reset: '⟳',
  delete: '🗑',
  archive: '🗄',
  // Same glyph as the pool drawer's "Session" button (`components/WorktreePool.tsx`).
  console: '🪟',
  // The live stack's web UI link, as a menu item too; the pool's 🌐.
  stack: '🌐',
  // Duplicate, the ⋯ item that creates: ⧉ for a copied record, monochrome like its neighbours.
  duplicate: '⧉',
  // "Move…" and "Waiting…" (`MoveToFeatureDialog`, `WaitOnCustomerDialog`).
  file: '⇥',
  wait: '⏸',
  // A comment's "Add reaction": monochrome, like its Edit/Delete neighbours; the palette holds the colour.
  react: '☺',
} as const

// Whether a row's cleanup action reads "Archive" rather than "Delete", from state alone, so
// TaskRow's button and Tasks.tsx's `deleteTask` confirm agree. Built on `LIVE_STATES` (`_LIVE_STATES`
// in `task_lifecycle.py`), not the transition table (spec R4.2). `shared.test.tsx` pins the row set.
//   1. a DRAFT (top-level or subtask) reads "Delete", never Archive;
//   2. a LIVE row reads "Delete" (deleting it stops its runner);
//   3. a non-epic row in a recoverable failure keeps "Delete"; an EPIC there reads "Archive" (recovered
//      per child; `retry` 400s an epic).
// Everything else — `done`, a system_failed/paused epic, a legacy state — archives.
export function isArchiveRow(t: PipelineTask): boolean {
  if (t.state === 'draft') return false
  if (LIVE_STATES.has(t.state)) return false
  const isRecoverableFailure = t.state === 'system_failed' || t.state === 'paused'
  if (isRecoverableFailure && t.persona !== 'epic') return false
  return true
}

// Edit is reachable in every state, including `active` (an epic's transition_mode can change mid-run).
// Mirrors `update_task`'s PUT guard: a runner reads a child's body at dispatch, so an edit applies to the
// next dispatch.
export function isEditable(_t: PipelineTask): boolean {
  return true
}

// One identifier convention, `task #<id>` (the Task id is the fleet's dispatch identity).
export function taskLabel(task: { id: number }): string {
  return `task #${task.id}`
}

// Whether a `done` Task should be flagged: a reason string, or null for a confirmed success.
//   • An EPIC (or legacy plannable parent) completes via children: flagged only with no children (a
//     false close), counted from `children` or `child_count`.
//   • Otherwise a null `outcome_pr_url` is the false-close signature — unless `outcome_no_pr_reason`
//     records a sanctioned no-PR terminal (the server's `_child_rollup_ready` accepts it).
export function doneWarnReason(task: PipelineTask): string | null {
  if (task.state !== 'done') return null
  if (task.persona === 'epic' || task.is_plannable) {
    const childCount = task.children != null ? task.children.length : (task.child_count ?? 0)
    return childCount === 0 ? 'done with no subtasks' : null
  }
  // A task with no repository never has a PR to confirm (`types.ts::PipelineTask.has_repo`).
  if (task.has_repo === false) return null
  if (!task.outcome_pr_url && !task.outcome_no_pr_reason) return 'done with no confirmed merged PR'
  return null
}
