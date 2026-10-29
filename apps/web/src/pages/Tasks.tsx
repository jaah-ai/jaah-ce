import { Suspense, lazy, startTransition, useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { replaceEqualDeep, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail, get, getPage, post } from '../api'
import { useAuth, useCan } from '../auth'
import { useViewerMode } from '../lib/viewerMode'
import Banner from '../ui/Banner'
import { useConfirm, type ConfirmOptions } from '../components/ConfirmDialog'
import { useBusyIds } from '../hooks/useBusyIds'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import { listWorktrees, useProjects } from '../hooks/useRosters'
import { useConsoleDock } from '../components/ConsoleDock'
import { DISPATCH_ROUTES, startBody, startIntentFor, type ForceStartBody, type StartIntent } from '../components/tasks/controls'
import { useDeleteTask, useTaskActions } from '../components/tasks/useTaskActions'
import { useOtherOrgOf } from '../ui/OrgIndicator'
import MoveToFeatureDialog from '../components/tasks/MoveToFeatureDialog'
import WaitOnCustomerDialog from '../components/tasks/WaitOnCustomerDialog'
import PriorityDialog from '../components/tasks/PriorityDialog'
import PreemptDialog, { preemptForecast } from '../components/tasks/PreemptDialog'
import { loadTaskDetailPage } from './loadTaskDetailPage'
import PaneHeader from '../components/tasks/pane/PaneHeader'
import {
  ACTION_ICON,
  buildTagCountsUrl,
  buildTaskCreatorsUrl,
  buildTasksUrl,
  displayTitle,
  type ExpandedEntry,
  isArchiveRow,
  LIVE_STATES,
  TASK_ACTIONS,
  TASK_LIST_MAX,
  TASK_PAGE_SIZE,
  readTaskSources,
  actAsNeededFor,
} from '../components/tasks/shared'
import { enterActAs, useActAsRows } from '../lib/actAs'
import type { TaskTreeGrouping } from '../components/tasks/taskTree'
import ActivityFeedView from '../components/tasks/listViews/ActivityFeedView'
import type { PaneSelection, TaskListCreate, TaskListStateGrouping } from '../components/tasks/listViews/types'
import FeatureHeader from '../components/tasks/pane/FeatureHeader'
import { PANE_QUERY_KEY, forgetDeletedTasks } from '../components/tasks/pane/queryKeys'
import { paneCountsUrl, type PaneFilters } from '../components/tasks/pane/boardCounts'
import { boardRowsEpoch, rememberBoardRows } from '../lib/taskPageSeed'
import { readMuted } from '../lib/taskMute'
import { STREAM_DOWN_POLL_MS, useLiveRefetch } from '../hooks/useLiveRefetch'
import { useEventStream } from '../hooks/useEventStream'
import { useBulkRuns } from '../hooks/useBulkRuns'
import { bulkIdentity } from '../lib/bulkRun'
import { withAdded, withRemoved } from '../lib/sets'
import BulkActionBar, { type BulkAction } from '../ui/BulkActionBar'
import BulkRunReports from '../ui/BulkRunReports'
import { clearPicks, createPickStore, togglePick, usePicking, usePicks, type PickStore } from '../lib/pickStore'
import { FORM_ACTION_BAR_ATTR } from '../ui/FormActionBar'
import { countOf } from '../lib/humanize'
import { scrollingAncestor } from '../lib/scrollWithinPane'
import {
  useCanCreateChats,
  useCanCreateTasks,
  useCanDeleteTaskRow,
  useCanDeleteTasks,
  useCanEditTaskRow,
  useCanEditTasks,
  useCanRunTaskRow,
} from '../hooks/useTaskAccess'
import { useAwaitingApprovalReason } from '../lib/orgLifecycle'
import { duplicateTask, type NewTaskNavState } from '../lib/taskDuplicate'
import { clearTaskDraft, inlineTaskDraftStorageKey, loadInlineTaskDraft } from '../lib/taskDraftStorage'
import { INLINE_TASK_PREFIXES, readInlinePrefixLine } from '../components/tasks/inlineTaskPrefix'
import type { FilingTarget } from '../components/tasks/featureFiling'
import {
  GROUP_BY_STATE,
  GROUP_PARAM,
  groupParamValue,
  isProjectGroupBy,
  isTagGroupBy,
  offeredGroupBys,
  parseGroupParam,
  resolveGroupBy,
} from '../lib/taskGrouping'
import {
  parseTaskDeepLinkParam,
  parseTaskDeepLinkProjectParam,
  TASK_DEEP_LINK_PARAM,
  TASK_DEEP_LINK_PROJECT_PARAM,
  taskRowElementId,
} from '../lib/taskDeepLink'
import {
  expandedTasksStorageKey,
  loadExpandedTaskIds,
  saveExpandedTaskIds,
} from '../lib/taskExpandStorage'
import { resolveDateRangeBounds, type TaskDateRangeId } from '../lib/taskDateRange'
import { useOfficialDayStart, useOfficialZone } from '../lib/officialZone'
import { orderCreators } from '../lib/taskCreators'
import { loadTaskFilters, saveTaskFilters, taskFiltersStorageKey } from '../lib/taskFilterStorage'
import {
  clearTaskUrlFilters,
  hasTaskUrlFilters,
  readTaskUrlFilters,
  writeTaskUrlFilters,
  type TaskUrlFilters,
} from '../lib/taskUrlFilters'
import { hasLiveSession, resolveTitle, runnerWorktreeFor, worktreeForTask } from '../lib/worktree'
import { publishBoardScope } from '../lib/boardScope'
import {
  ALL_SCOPE,
  SCOPE_PARAM,
  locateScope,
  parseScope,
  scopeHref,
  scopeKey,
  scopeProjectId,
  scopeToListParams,
  taskInScope,
  type TaskScope,
} from '../lib/taskScope'
import { TASK_TREE_QUERY_KEY, useTaskTree } from '../hooks/useTaskTree'
import { useFocusedProjectId } from '../hooks/useFocusedProjectId'
import { focusedLandingScope } from '../lib/focusedProject'
import { FOCUS_COMPOSER_STATE } from '../components/tasks/rail/TaskTree'
import { ABOVE_PHONE, SHELL_FILL_ATTR, TASK_BOARD_CONTAINER_SX } from '../ui/listStyles'
import QueuedOverCap from './ce/QueuedOverCap'
import { STARTER_MAX_TASKS } from './templates/sections'
import { LAUNCHER_CLEARANCE_PX, useLauncherYield } from '../ui/launcherSeat'
import type {
  DispatchForecast,
  OrgRef,
  PipelineTask,
  Project,
  TagCounts,
  TagGroup,
  TaskCreator,
  TaskSourceOption,
  TaskTreeCounts,
  Worktree,
} from '../types'

/* The board's starters — the setup diagram, then Most popular templates (`TemplateStarters`): their own chunk. */
const TemplateStarters = lazy(() => import('./templates/TemplateStarters'))

// AC1c.3: the create/edit form lives on the standalone page (`/tasks/new`, `/tasks/:id`); this route neither
// imports its ~500 KB TipTap chunk nor holds any modal state. It only PREFETCHES that page's chunk once mounted
// (`loadTaskDetailPage`), so the first row press opens a page already downloaded.

const METERED_REFETCH_MIN_MS = 60_000

/* The board's own floor between live refetches. Each one re-reads the list, every expanded row's detail,
   worktrees and both counts (hundreds of KB on a big board), and the shared debounce bounds a burst only while
   it stays dense: events a second apart, as one busy chat parking and resuming every turn, re-read the whole
   board for every open tab once a second. The first event still refetches at once. */
const TASKS_LIVE_REFETCH_MIN_MS = 5_000

const NO_PROJECTS: Project[] = []

/* A quiet hint above the list; colour from the `caption` variant. */
const NOTE_SX = { color: 'text.secondary', mb: '0.5rem' } as const

// The `/tasks` page — the operator's console over the epic/`persona` Task pipeline. A thin container:
// list/expanded state, fetch + live-refetch + poll fallback, and writes; rendering is `components/tasks/*`.
/** Focus the inline composer once it mounts (it may land a frame after a navigation), retrying briefly. Returns
 *  the cancel. */
function focusComposerSoon(): () => void {
  let tries = 0
  const timer = setInterval(() => {
    const input = document.querySelector<HTMLInputElement>('[data-testid="inline-new-task-input"]')
    if ((input && !input.disabled) || ++tries > 20) {
      input?.focus()
      clearInterval(timer)
    }
  }, 100)
  return () => clearInterval(timer)
}

/* THE /tasks PAGE HUGS THE SIDEBAR: above the phone band it cancels all but 8px of `.app-main`'s 2rem
   left inset (`App.tsx`'s layout contract), so the header and the list's status discs sit just
   clear of the sidebar instead of 37px off it. The phone keeps the shell's 1rem: no sidebar sits beside it there.
   It is also the BOARD's query container: the header's one line and the activity row's drop order follow its
   width, which the docked console narrows without the window changing (`ui/listStyles.ts`'s `BOARD_*`).
   ABOVE THE PHONE IT FILLS THE SHELL: a column of the header, the list's OWN scroll region and the bulk dock
   under it (`SHELL_FILL_ATTR`, so `.app-main` keeps its plain bottom inset). The dock used to stick to `.app-main`'s
   foot with rows sliding beneath it; now no row is ever under it, and it spans the list's width. The phone keeps the
   shell's scroll (its bottom nav and onboarding bar are sized against `.app-main`). */
const TASKS_PAGE_SX = {
  ...TASK_BOARD_CONTAINER_SX,
  [ABOVE_PHONE]: { ml: 'calc(8px - 2rem)', height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' },
} as const

/* The list's scroll region above the phone (see `TASKS_PAGE_SX`). Its bottom inset is the Help launcher's clearance
   less the shell's own 1.75rem, so the last row's `⋯` scrolls clear of the orb; with the dock up the orb is hidden
   (`useLauncherYield`). Its scrollport clips sideways too, so it reaches 4px back into the page's inset and pads it
   in again: a selected disc's ring, which overhangs the row by 1px, stays whole. */
const LIST_SCROLL_SX = {
  [ABOVE_PHONE]: {
    flex: 1, minHeight: 0, overflowY: 'auto', ml: '-4px', pl: '4px',
    pb: `calc(${LAUNCHER_CLEARANCE_PX}px - 1.75rem)`,
  },
} as const

/* Sticky at the scrollport's bottom on the phone, on the page ground so rows slide under it (as the pane's sticky
   header); above it the dock simply follows the list's region. The bar spans the list's width and the
   launcher hides while it is up (`useLauncherYield`): seated level with it the 56px orb overhung the bar's
   right border, and lifted above it the orb covered the last row's pick box and `⋯`. */
const BULK_DOCK_SX = {
  position: 'sticky',
  bottom: 0,
  zIndex: 3,
  bgcolor: 'background.default',
  py: 1,
  display: 'flex',
  flexDirection: 'column',
  gap: 1,
  [ABOVE_PHONE]: {
    position: 'static',
    flexShrink: 0,
    '&&': { paddingRight: 0 },
  },
} as const

/* The board's bulk verbs as a closed set (`useBulkRuns`): the id keys the button, its run and its report. */
const BULK_DELETE = 'delete'
const BULK_ARCHIVE = 'archive'
type BulkVerbId = typeof BULK_DELETE | typeof BULK_ARCHIVE
/* How long the pick must sit still before the board re-renders for it (the ⋯ menus' word): longer than the gap
   between two quick clicks, so a burst re-renders ticks only. */
const PICK_ECHO_DELAY_MS = 400
/* How long the note on a pick a draw dropped stays: long enough to read, never a fixture. */
const PICK_NOTE_MS = 6_000
/* Not a verb on rows: it grows the pick (`selectAllMatching`). */
const SELECT_ALL = 'select-all'
/* The read behind Select all — every row the board's filters match, up to `TASK_LIST_MAX`. */
const MATCHING_QUERY_KEY = ['tasks', 'matching'] as const

/** A bulk report's row word. Module-level, so `runBulk` keeps its identity. */
/* A FRESH LIST PAGE KEEPS EVERY UNCHANGED ROW'S IDENTITY: each poll's JSON is all new objects, so no
   memoized row could skip its render. Per row by id, deep-equal to the held row → the held object. */
function keepUnchangedRows(prev: readonly PipelineTask[], next: PipelineTask[]): PipelineTask[] {
  const held = new Map(prev.map(task => [task.id, task]))
  return next.map(task => {
    const old = held.get(task.id)
    return old ? replaceEqualDeep(old, task) : task
  })
}

const bulkTaskIdentity = (task: PipelineTask): string => `#${task.id} ${displayTitle(task.title)}`

/* THE DOCK READS THE PICK ITSELF (`lib/pickStore.ts`): its count and verb labels follow every click without the
   board re-rendering. `actionsFor` is the page's (its closures reach the confirm, the runs and the refetch). */
function TaskBulkDock({ store, actionsFor, onClear, runs, reports }: {
  store: PickStore<PipelineTask>
  actionsFor: (picked: PipelineTask[]) => BulkAction[]
  onClear: () => void
  runs: ComponentProps<typeof BulkRunReports>['runs']
  reports: ComponentProps<typeof BulkRunReports>['reports']
}) {
  const picks = usePicks(store)
  const actions = actionsFor([...picks.values()])
  return (
    <>
      <BulkActionBar count={picks.size} onClear={onClear} actions={actions} label="Bulk task actions" />
      <BulkRunReports actions={actions} runs={runs} reports={reports} />
    </>
  )
}

export default function Tasks() {
  // "New task" and a row's Edit are NAVIGATIONS to the standalone page.
  const navigate = useNavigate()
  const viewer = useViewerMode()
  const [tasks, setTasks] = useState<PipelineTask[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<Record<number, ExpandedEntry>>({})
  const [fetching, setFetching] = useState<Set<number>>(new Set())
  const [submitting, withBusy] = useBusyIds<number>()
  const [actionError, setActionError] = useState<{ taskId: number; text: string } | null>(null)
  /* A row action's write: the row is busy for its duration, and a rejection lands in the page's banner (through
     `apiDetail`), naming the task — never as a line under the row. Only the same task's next write
     clears it, so acting on another row never hides an unread failure. Never rejects. */
  const rowWrite = (task: PipelineTask, fallback: string, write: () => Promise<unknown>) =>
    withBusy(task.id, async () => {
      setActionError(prev => (prev?.taskId === task.id ? null : prev))
      try {
        await write()
      } catch (err) {
        setActionError({ taskId: task.id, text: `#${task.id}: ${apiDetail(err, fallback)}` })
      }
    })
  // Every PROJECT this operator may see, fetched once: the delivery join (`projectNameById` — a task's worktree
  // repo keys off `task.project_id`) and the create rows' roster. Seeing a project is not being able to file into it: every create
  // rung also asks the grant map (`resolveCreateProject` in `components/tasks/shared.tsx`). Independent of the
  // list so it never blocks it.
  // Not awaited by fetchTasks; failure costs name lookups and the create row shows its no-project hint.
  const projectsQuery = useProjects()
  const allProjects = Array.isArray(projectsQuery.data) ? projectsQuery.data : NO_PROJECTS
  // Did that fetch ANSWER: `[]` alone is pending, failed or a new org with no project (the create row tells them apart).
  const projectsAnswered = Array.isArray(projectsQuery.data)
  // R5: the live worktree set, refreshed on the list's live-refetch/poll paths so a freshly dispatched task
  // links to its worktree. Independent of the list.
  const [worktrees, setWorktrees] = useState<Worktree[]>([])

  /* ── TAG GROUPING ──────────────────────────────────────────────────────────
     THE CATALOG IS FETCHED, not derived from loaded rows (which would reshuffle sections while scrolling):
     `GET /api/tags/groups` serves ACTIVE groups in `position` order. Once, on mount (groups change on Settings).
     THE MARKER RECORDS *WHOSE* CATALOG WE HOLD, because the reconciliation below DELETES a stored selection:
       · `undefined` = nothing answered yet, INCLUDING A FAILED FETCH — "no active groups" and "unreachable" look
         alike, and one transient 500 must not delete a chosen grouping (meanwhile the board renders ungrouped);
       · the ADMIN ID it was fetched for, so an admin switch never reconciles B's selection against A's catalog. */
  const [tagGroups, setTagGroups] = useState<TagGroup[]>([])
  const [tagCatalogAdminId, setTagCatalogAdminId] = useState<string | null | undefined>(undefined)

  /* THE SECTION TOTALS COME FROM THE SERVER (`GET /api/tasks/tag-counts`, same row set as the list): a count over
     the lazy 50-row window would lie. Refetched on the list's own events. `null` until it lands — no number
     rather than an unbacked zero. */
  const [tagCounts, setTagCounts] = useState<TagCounts | null>(null)

  /* FOLDED SECTIONS, keyed by TAG ID (`''` = Untagged), held HERE: sentinel appends can change the section set
     (index keys would move folds), and a remounted child would re-open. In memory only. */
  const [sectionCollapsed, setSectionCollapsed] = useState<Record<string, boolean>>({})
  // FOCUS is restored from `sessionStorage` (`lib/taskFilterStorage.ts` says why the SESSION; everything else
  // the board shows lives in the URL, below). Restored IN THE STATE INITIALIZER, not an effect: the mount fetch would otherwise request and paint the unfiltered board first.
  // `admin` is available this early (`auth.tsx` seeds from the persisted snapshot); the restore effect below
  // covers a late or changed id.
  const { admin, updatePreferences } = useAuth()
  const filtersStorageKey = taskFiltersStorageKey(admin?.id ?? null)
  // ONE read at mount (lazy initializer).
  const [restoredFilters] = useState(() => loadTaskFilters(filtersStorageKey))

  /* ── AND THE ROW-REMOVING FILTERS, WHICH ARE THE URL'S ───────────────────────────────────────────
     Search, period, creator picks and Hidden are read from and written to the query string
     (`lib/taskUrlFilters.ts` is the single home): a narrowed board is a link one operator sends another. One
     owner per value. `useSearchParams` answers on the FIRST render, so the first request still carries them;
     a bare `/tasks` opens unfiltered (operator's call).
     DECLARED ONCE, above every reader: the deep-link consumer below shares it (two calls = two writers). */
  const [searchParams, setSearchParams] = useSearchParams()
  /* Memoised on the router-memoised params, so the object and creator ARRAY keep identity for effect keys. */
  const urlFilters = useMemo(() => readTaskUrlFilters(searchParams), [searchParams])
  const { q, showHidden, dateRange, dateFrom, dateTo, creatorIds, sources, state: stateFilter } = urlFilters
  /* A SCALAR for the fetch effects' deps: non-filter URL changes (the deep-link strip) hand back a fresh array
     with identical contents. Same idiom as `expandedIdsKey`. */
  const creatorKey = creatorIds.join(' ')
  const sourceKey = sources.join(' ')

  /* THE NEWEST PARAMS THIS PAGE KNOWS OF — the router's, or those a write of ours just issued. EVERY write builds
     on this, never on its closure.
     `setSearchParams` DOES NOT COMPOSE: react-router's functional form resolves against THIS RENDER's params
     and navigates immediately (react-router-dom 6.30), so two patches in one gesture lose the first silently.
     That is the Clear gesture (four filters in one press, `clearFilters` below), which otherwise left
     the URL and board narrowed. Effect closures (the creator reconcile) are stale the same way, hence a ref.
     RE-SYNCED WHEN THE LOCATION LANDS, so a stale base cannot resurrect the stripped `?task=`. */
  const liveParamsRef = useRef(searchParams)
  useEffect(() => { liveParamsRef.current = searchParams }, [searchParams])

  /* THE ONE WRITER for every URL change this page makes. A PICK PUSHES, so Back/Forward walk the selection,
     filters, grouping and view step by step; typing in search REPLACES (one entry per term, not per pause).
     IT COMPOSES via the ref above. `resetWindow` resets the lazy window in the same event — React batches the
     router commit and `setLimit`, so one request per change. */
  function writeUrl(next: URLSearchParams, { replace = false, resetWindow = true } = {}) {
    liveParamsRef.current = next
    setSearchParams(next, { replace })
    if (resetWindow) setLimit(TASK_PAGE_SIZE)
  }

  function patchUrlFilters(next: Partial<TaskUrlFilters>, { replace = false } = {}) {
    const base = liveParamsRef.current
    writeUrl(writeTaskUrlFilters({ ...readTaskUrlFilters(base), ...next }, base), { replace })
  }

  /* ── THE SELECTION (`lib/taskScope.ts`) ──────────
     `?scope=` is the board's SOURCE OF TRUTH: the rail's links (the phone's scope dropdown is the same tree) set it, it
     survives a reload and a shared link, and Back walks it. ABSENT (or junk, or a retired key), the board opens
     on All projects. No other param narrows it: an unknown one such as a retired filter is simply not read. The
     row set is `scopeToListParams`. */
  const { tree: taskTree } = useTaskTree(true)
  const rawScope = searchParams.get(SCOPE_PARAM)
  /* THE FOCUSED PROJECT (`lib/focusedProject.ts`): a `/tasks` with no `?scope=` — opening Jaah, `/` redirects
     here — lands on it. Read as the selection at once (so the first fetch is the focused project's, not All
     projects') and written into the URL, REPLACED so Back does not bounce through All projects. A `?task=` deep
     link moves the selection itself (below) and wins. */
  const focusedProjectId = useFocusedProjectId()
  /* A bare `?project=N` (no `?task=`, no `?scope=`) names the project to open on, and wins over the focus: left
     unread, the board — and ⤢'s editor after it — opened on some other project. */
  const linkedProjectId = rawScope === null && !searchParams.has(TASK_DEEP_LINK_PARAM)
    ? parseTaskDeepLinkProjectParam(searchParams.get(TASK_DEEP_LINK_PROJECT_PARAM))
    : null
  const landingScope = searchParams.has(TASK_DEEP_LINK_PARAM)
    ? null
    : linkedProjectId !== null
      ? scopeKey({ kind: 'project', projectId: linkedProjectId })
      : focusedLandingScope(rawScope, focusedProjectId, taskTree?.projects.map(p => p.id))
  const effectiveRawScope = landingScope ?? rawScope
  const scope: TaskScope = useMemo(() => parseScope(effectiveRawScope), [effectiveRawScope])
  useEffect(() => {
    if (landingScope === null) return
    const next = new URLSearchParams(liveParamsRef.current)
    next.set(SCOPE_PARAM, landingScope)
    next.delete(TASK_DEEP_LINK_PROJECT_PARAM)
    writeUrl(next, { replace: true })
  }, [landingScope])
  /* THE ONE PROJECT the selection lives in — what new tasks are filed into and what the create ladder calls the
     board's project — or null for All projects or a feature the tree has not named yet. */
  const boardProjectId = scopeProjectId(scope, taskTree)
  /* A feature scope's feature, for the inline create rows to file into. A SHIPPED one is carried too: the API
     refuses filing into it, so the rows file to its project and say so (`resolveCreateTarget`). */
  const scopedFeatureLocation = scope.kind === 'feature' ? locateScope(scope, taskTree) : {}
  /* Memoised on its four facts, so the create bundle (`listCreate`) keeps its identity across a refetch. */
  const scopedFeatureId = scopedFeatureLocation.feature?.id
  const scopedFeatureName = scopedFeatureLocation.feature?.name
  const scopedFeatureStatus = scopedFeatureLocation.feature?.status
  const scopedFeatureProjectId = scopedFeatureLocation.project?.id
  const scopedFeature = useMemo(
    () => scopedFeatureId !== undefined && scopedFeatureName !== undefined && scopedFeatureStatus !== undefined &&
      scopedFeatureProjectId !== undefined
      ? { id: scopedFeatureId, name: scopedFeatureName, projectId: scopedFeatureProjectId, status: scopedFeatureStatus }
      : undefined,
    [scopedFeatureId, scopedFeatureName, scopedFeatureProjectId, scopedFeatureStatus],
  )
  /* WHAT ⤢ HANDS `/tasks/new` BESIDE THE PROJECT: the scope's ACTIVE feature, so the editor files where the row
     said it would (`TaskDetailPage` re-validates it against the tree). */
  const boardFeatureId = scopedFeature?.status === 'active' ? scopedFeature.id : null
  /* `/tasks/new`'s navigation state: the board's project and feature, so ▶ and ⤢ file into the same place. */
  const newTaskState: NewTaskNavState = { boardProjectId, boardFeatureId }
  /* The create row's project pick (`useInlineTaskCreate`'s picker), held HERE so ⤢ and ＋ file where the row says.
     A new scope is a new board: the pick does not follow it. */
  const [createPick, setCreatePick] = useState<number | null>(null)
  // Where the row would file now (`useInlineTaskCreate` writes it): ＋ opens the editor there.
  const createTargetRef = useRef<FilingTarget | null>(null)
  useEffect(() => { setCreatePick(null) }, [effectiveRawScope])
  const expandState = (body: string, filingTarget: FilingTarget | null): NewTaskNavState =>
    ({ body, ...newTaskState, ...(filingTarget ? { filingTarget } : {}) })
  /* ＋ TAKES THE ROW'S WORDS TOO: the line typed but not filed is a MOVE into the editor, as ⤢ —
     the draft is discarded here, else it waits one ▶ away from filing twice. A Chat chip's words go without it. */
  const takeInlineDraft = (): string => {
    const key = inlineTaskDraftStorageKey(admin?.id ?? null)
    // Only the prefixes the row offers (a chat chip needs the chat gate), as the row itself reads the line.
    const offered = INLINE_TASK_PREFIXES.filter(p => p.intent !== 'chat' || canCreateChats)
    const { body } = readInlinePrefixLine(loadInlineTaskDraft(key), offered)
    clearTaskDraft(key)
    return body.trim()
  }
  /* The row menu's two filing dialogs ("Move…", "Waiting…"), page-mounted like
     `confirmDialog` so a refetch re-keying the rows cannot unmount an open one. */
  const [filingTask, setFilingTask] = useState<PipelineTask | null>(null)
  const [waitingTask, setWaitingTask] = useState<PipelineTask | null>(null)
  /* The row menu's "Priority…", and the preempt it may offer after: the deleted Queue style's bump that
     sorts ahead of running work asked the forecast, rather than leaving the row silently queued. */
  const [priorityTask, setPriorityTask] = useState<PipelineTask | null>(null)
  const [priorityPreempt, setPriorityPreempt] = useState<{ task: PipelineTask; forecast: DispatchForecast } | null>(null)
  /* The task page's chunk, fetched once the board is up. A failed prefetch is silent: the row press's own
     `lazy()` load asks again and reports there. */
  useEffect(() => {
    loadTaskDetailPage().catch(() => {})
  }, [])
  /* THE USER'S MUTES (`lib/taskMute.ts`): the muted nodes above the scope leave its row set. A STRING for memo
     and effect deps, as `creatorKey`. */
  const mutedKey = readMuted(admin?.preferences).join(' ')
  const muted = useMemo(() => (mutedKey === '' ? [] : mutedKey.split(' ')), [mutedKey])
  const listScope = scopeToListParams(scope, muted)
  const listScopeKey = listScope.toString()
  // "Show hidden" — the menu's ONE additive switch: hidden AND archived rows together, on one tick (one boolean
  // end to end). Off by default.
  // MOSTLY SYSTEM ROWS, NOT ONLY: a Runner cron's `hidden` tick (`app/cron_activity_runner.py`) files the
  // operator's own maintenance hidden. Both classes land in the board's FINISHED half (a hidden row asking a
  // question is on the board anyway, unfinished)
  // (`routes/tasks/listing.py::_TASK_LIST_COMPLETED_GROUP`, client half `isCompletedRow`), so ticking this never
  // displaces live work — at the cost of sorting an active maintenance run with finished business.
  // `buildTasksUrl` in `components/tasks/shared.tsx` is the single home for what it sends
  // (`include_hidden` + `include_archived`). The value is the URL's (`urlFilters`).

  /* ── THE FOCUS TOGGLE ──────────────────────────
     Shows only what the operator can act on (running, question, paused, draft) — the strongest narrowing here,
     a switch in the Filter & view menu (see `TaskToolbar`). Top-level QUEUED rows stay visible (work lined up, with
     nothing standing in for it); queued CHILDREN are hidden.
     SERVER-SIDE for top-level rows, CLIENT-SIDE for an expanded parent's children — argued at `setFocusParam`
     (`components/tasks/shared.tsx`). `_FOCUS_TOP_LEVEL_STATES` and `_FOCUS_STATES`
     (apps/api/app/task_lifecycle.py) are the single home for which states survive; the child half lands via
     `isFocusRow`. REMEMBERED PER TAB for the session; OFF on a fresh tab. */
  const [focus, setFocus] = useState(restoredFilters.focus)

  /* ── THE PERIOD FILTER ───────────────────────────────────────────────────────
     A WINDOW on `updated_at` (the axis the board sorts and ages by). SERVER-SIDE: filtering the lazy 50-row page
     client-side would leave everything behind it unfiltered. `resolveDateRangeBounds` → `buildTasksUrl`
     (`lib/taskDateRange.ts` owns the vocabulary and calendar alignment). All three values are the URL's; the
     two bounds ride it even under a preset so a trip through Recent keeps typed dates (`lib/taskUrlFilters.ts`
     is the single home). */
  /* THE CALENDAR IS THE CONTROL PLANE'S ZONE, never this browser's (`lib/officialZone.ts`;
     `lib/taskDateRange.ts` says why board and feed must agree). A dep of both fetch effects. */
  const zone = useOfficialZone()
  /* WHEN THAT ZONE'S DAY LAST TURNED — a fetch-effect dep only, so a quiet board's `Today` window moves at
     midnight (`dateBounds()` already reads the clock per request). `lib/officialZone.ts` owns the timer. */
  const dayStart = useOfficialDayStart(zone)
  /* RESOLVED AT REQUEST TIME, never memoised on the controls: `Today`'s floor depends on the CLOCK, so a memo
     would send yesterday's window (and stale section totals) after midnight. It is cheap. */
  function dateBounds() {
    return resolveDateRangeBounds(dateRange, dateFrom, dateTo, zone)
  }

  /* WHOSE ROWS — creator ids, EMPTY for everybody. A real filter, the URL's. Two surfaces drive it (the header's
     avatar strip and the menu's Creator page, operator's call) and share one setter and one roster. */
  /* THE ROSTER (`GET /api/tasks/creators`): people with top-level tasks in the CURRENT PROJECT SCOPE. Fetched,
     never derived from the lazy window (as `tagGroups`). Fire-and-forget: a failure costs only the picker. */
  const [creators, setCreators] = useState<TaskCreator[]>([])
  /* THE SOURCE VOCABULARY (`GET /api/tasks/sources`): codes and the server's labels, which are never spelled
     here. Fire-and-forget like the roster: a failure costs only the picker and the chips' words. */
  const [taskSources, setTaskSources] = useState<TaskSourceOption[]>([])

  /* ── THE VIEW: GROUP BY ──────────
     The URL's (`?group=`), so a link reproduces the view; State by default. `effectiveGroupBy` is what this
     selection can honour (`resolveGroupBy`): Project only across projects, a tag group only while the catalog
     holds it. */
  const groupBy = parseGroupParam(searchParams.get(GROUP_PARAM))
  const groupByOptions = useMemo(() => offeredGroupBys(scope.kind === 'all', tagGroups), [scope.kind, tagGroups])
  const effectiveGroupBy = resolveGroupBy(groupBy, groupByOptions, tagCatalogAdminId !== undefined)
  /* Sectioning by State reads the server's counts (`stateGrouping` below). */
  const feedStateGrouped = effectiveGroupBy === GROUP_BY_STATE
  /* The Live Cast viewer has no preferences to keep the fold in, and a visitor came to see finished work: Done
     opens there by default, and the fold lives for the page. */
  const [viewerDoneExpanded, setViewerDoneExpanded] = useState(true)
  const doneExpanded = viewer ? viewerDoneExpanded : admin?.preferences?.task_done_expanded === true
  /* THE STATE BOARD'S WINDOW in the order it is drawn (`buildTasksUrl` says how): a folded Done costs no row. */
  /* AN EXPLICIT STATE: DONE FILTER OPENS DONE: folded, the window sent `state=done&exclude_done=true` — no
     row — while Select all named the counts' total, and a pick under a fold is dropped (`onDrawn`). */
  const doneOpen = doneExpanded || stateFilter === 'done'
  const windowShape = { sectionOrder: feedStateGrouped, excludeDone: feedStateGrouped && !doneOpen }
  const queryClient = useQueryClient()
  // The lazy-load window: rows ASKED for, growing a page at a time via the sentinel and reset on filter change.
  // `shared.tsx`'s `buildTasksUrl` doc says why the window GROWS rather than stitching offset pages.
  const [limit, setLimit] = useState(TASK_PAGE_SIZE)
  /* A NEW SELECTION RESETS THE WINDOW, whoever moved it — the sidebar's (and the phone dropdown's) links navigate
     without `writeUrl`. Reset DURING RENDER, so the fetch sees the reset limit: one request per change. */
  const windowScopeKey = scopeKey(scope)
  const [limitScopeKey, setLimitScopeKey] = useState(windowScopeKey)
  if (limitScopeKey !== windowScopeKey) {
    setLimitScopeKey(windowScopeKey)
    setLimit(TASK_PAGE_SIZE)
  }
  // Expand state is persisted to `localStorage` under ADMIN-SCOPED keys (`lib/taskExpandStorage.ts` owns the
  // storage shape); this page owns only when to read and write.
  const can = useCan()
  const canReadWorktrees = can('worktrees:view')
  // The three task WRITE keys: `tasks:view` implies none, so a rendered row or board is no evidence of any
  // write. `hooks/useTaskAccess.ts` single-homes which key covers which verb; hence one `undefined` per affordance.
  const canEditTasks = useCanEditTasks()
  const canDeleteTasks = useCanDeleteTasks()
  const canCreateTasks = useCanCreateTasks()
  /* The narrower CHAT answer — `tasks:create` AND `worktrees:edit` (the operator must type back into the
     console). Argued in `hooks/useTaskAccess.ts`. Gates both New Chat and the inline 💬. */
  const canCreateChats = useCanCreateChats()
  /* ...AND THE TWO PER-ROW ANSWERS: an author may edit/delete their own `draft` rows (`authored_draft`), so one
     board can mix editable and non-editable rows. The flat keys still decide whether a handler exists at all,
     and alone answer Start/Stop/Reset/Complete (`hooks/useTaskAccess.ts`). */
  const canEditRow = useCanEditTaskRow()
  const canDeleteRow = useCanDeleteTaskRow()
  const canRunRow = useCanRunTaskRow()
  const awaitingApproval = useAwaitingApprovalReason()
  const expandedStorageKey = expandedTasksStorageKey(admin?.id ?? null)
  // Write-through ARMED only once the restore for the current key settled — else the empty mount state would
  // overwrite the outline being restored.
  const restoredExpandedKeyRef = useRef<string | null>(null)
  // The filters' arming gate, seeded with the MOUNT key (restored synchronously): armed from the first render
  // (a no-op re-save); a late or changed admin id hands the restore effect the job.
  const restoredFiltersKeyRef = useRef(filtersStorageKey)
  // In-app confirm (spec R3) for destructive actions; `confirmDialog` renders once at the end.
  const [confirm, confirmDialog] = useConfirm()
  const sendTaskDelete = useDeleteTask()
  const otherOrgOf = useOtherOrgOf()
  // The console lives in the shell (components/ConsoleDock.tsx); this page resolves only which slot to open.
  // `kind` unset = the dock's default 'clauded' (the only kind any surface opens). `nodeId` is part of the
  // slot's identity: (project, number) is unique PER NODE, and the Title editor needs a node-addressed endpoint.
  const { open: openDockConsole } = useConsoleDock()
  // BA R9 — the Title → task deep link (`/tasks?task=<id>`, from `components/WorktreePool.tsx`). State so the
  // link never "works" while showing nothing:
  //   `deepLinkedId` — the revealed row, SELECTED until the operator's next interaction (not a timed flash).
  //   `pinnedTask`   — the target's top-level ancestor when outside the loaded window, pinned in.
  //   `deepLinkError`— a VISIBLE "no longer exists" state, never a silent unfiltered list.
  const [deepLinkedId, setDeepLinkedId] = useState<number | null>(null)
  const [pinnedTask, setPinnedTask] = useState<PipelineTask | null>(null)
  const [deepLinkError, setDeepLinkError] = useState('')

  // R6: overlapping fetches can resolve out of order; each path stamps a generation and applies only if still
  // latest. Independent counters, because the paths write disjoint state (`tasks` vs `expanded`).
  const listFetchGenRef = useRef(0)
  /* The keyset cursor past the window's last row (`X-Next-Cursor`), null when the server has no more. Every list
     answer — a refetch of the whole window or a scrolled page — replaces it. */
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  /* The window size a scrolled page just reached: the fetch effect below skips that ONE `limit` change, since the
     page already holds those rows. Any other change of `limit` (a reset) is a refetch. */
  const appendedLimitRef = useRef<number | null>(null)
  /* The list read in flight: a newer one ABORTS it — its answer would be discarded by the generation anyway.
     EXCEPT THE SAME READ ASKED AGAIN by the filter effect (`reuse`): StrictMode's re-run and a re-render
     that changed no filter join the read in flight, so the first load is ONE request. Live
     refetches never reuse — their point is an answer newer than the write that triggered them. */
  /* A SAME-URL READ IS NEVER ABORTED: a busy project's `task` events refetch at most a second apart
     (`useLiveRefetch`'s max wait), and a phone's first read can take longer — each refetch aborted the one before,
     so the board sat on skeletons until the events paused. A read of ANOTHER url (a filter, a scope, a page past
     the cursor) still aborts every read in flight: those answers could only be discarded. */
  const listAbortsRef = useRef(new Set<AbortController>())
  const abortListReads = () => {
    for (const controller of listAbortsRef.current) controller.abort()
    listAbortsRef.current.clear()
  }
  /* READS OF ONE URL RUN SIDE BY SIDE: a live refetch never joins a read already in flight — one that
     started before a write would paint the board as it stood before it. `reuse` alone joins the newest. */
  type ListRead = { url: string; read: Promise<{ data: PipelineTask[]; next: string | null }>; controller: AbortController }
  const listReadsRef = useRef<ListRead[]>([])
  function listPage(url: string, reuse = false) {
    const same = listReadsRef.current.filter(entry => entry.url === url && !entry.controller.signal.aborted)
    const newest = same[same.length - 1]
    if (newest && reuse) return newest.read
    if (same.length === 0) abortListReads()
    const controller = new AbortController()
    listAbortsRef.current.add(controller)
    const read = getPage<PipelineTask[]>(url, { signal: controller.signal })
    const entry = { url, read, controller }
    listReadsRef.current = [...listReadsRef.current, entry]
    const settle = () => {
      listAbortsRef.current.delete(controller)
      listReadsRef.current = listReadsRef.current.filter(held => held !== entry)
    }
    read.then(settle, settle)
    return read
  }
  /* WHICH WINDOW THE BOARD NOW DRAWS AN ANSWER FOR, and which one the latest list read asks (`null`: a read that
     is not a window — Select all's, a page past the cursor). An OLDER read of the latest url paints while the board
     draws no answer for it yet: its rows are the right set, only a moment older, and the newer read
     replaces them when it lands. Once the board draws that url, older answers are discarded as before — one would
     undo a row a write just patched in place. */
  const paintedListUrlRef = useRef<string | null>(null)
  const latestListUrlRef = useRef<string | null>(null)
  const detailFetchGenRef = useRef(0)
  // A third for section totals: a fast A→B scope switch could otherwise leave A's totals over B's rows.
  const countsFetchGenRef = useRef(0)

  function listUrl(size: number) {
    return buildTasksUrl(listScope, showHidden, size, dateBounds(), creatorIds, focus, q, sources, windowShape, stateFilter)
  }

  async function fetchTasks(reuse = false) {
    const gen = ++listFetchGenRef.current
    const epoch = boardRowsEpoch()
    const url = listUrl(limit)
    latestListUrlRef.current = url
    try {
      const { data, next } = await listPage(url, reuse)
      // Superseded by a newer fetch — discarded, unless it is the first answer for the window still asked.
      const firstForLatest = url === latestListUrlRef.current && paintedListUrlRef.current !== url
      if (gen !== listFetchGenRef.current && !firstForLatest) return
      paintedListUrlRef.current = url
      setTasks(prev => keepUnchangedRows(prev, data))
      rememberBoardRows(data, epoch)
      setNextCursor(next)
      setError('')
      setLoading(false)
    } catch {
      if (gen !== listFetchGenRef.current) return // stale failure — a newer fetch owns the state
      setError('Failed to load tasks')
    } finally {
      // Only the latest fetch clears the initial loading state.
      if (gen === listFetchGenRef.current) setLoading(false)
    }
  }

  // Refetch expanded parents' details so their rollups stay live. Both guards needed (R6): the generation
  // stamp, and `id in prev` so a detail collapsed mid-fetch is never resurrected.
  async function refetchExpandedDetails() {
    const ids = Object.keys(expanded).map(Number)
    if (ids.length === 0) return
    const gen = ++detailFetchGenRef.current
    await Promise.all(
      ids.map(async id => {
        try {
          const detail = await get<PipelineTask>(`/api/tasks/${id}`)
          if (gen !== detailFetchGenRef.current) return // superseded batch — discard
          setExpanded(prev => (id in prev ? { ...prev, [id]: detail } : prev))
        } catch {
          if (gen !== detailFetchGenRef.current) return // stale failure — a newer batch owns the state
          setExpanded(prev => (id in prev ? { ...prev, [id]: { error: 'Failed to load task detail' } } : prev))
        }
      }),
    )
  }

  // The tag-group catalog: fire-and-forget. The marker is stamped ONLY on success, with its admin (a failure
  // is not an answer — see the state declaration).
  async function fetchTagGroups() {
    const forAdminId = admin?.id ?? null
    try {
      const data = await get<TagGroup[]>('/api/tags/groups')
      setTagGroups(Array.isArray(data) ? data : [])
      setTagCatalogAdminId(forAdminId)
    } catch {
      // Keep the held catalog and the marker; nothing reconciles against a catalog that never arrived.
    }
  }

  /* The feed's State totals (`feedStateCounts`) on the live/poll paths only: a filter change moves the query's
     key, which fetches by itself — invalidating there too would cancel and repeat that request. */
  function refetchFeedStateCounts() {
    if (feedStateGrouped) void queryClient.invalidateQueries({ queryKey: [...PANE_QUERY_KEY, 'counts'] })
  }

  // Section totals, SKIPPED while Group by is None (this runs every refetch and poll). Error-tolerant: a
  // missing count renders a header without a number.
  async function fetchTagCounts() {
    // A TAG group only: recency sections rows already held and needs no server count.
    if (!isTagGroupBy(effectiveGroupBy)) return
    const gen = ++countsFetchGenRef.current
    try {
      // Through the shared builder (`shared.tsx`): totals must count the SAME row set as the list, and the
      // counting route must NOT take `limit`.
      const data = await get<TagCounts>(
        buildTagCountsUrl(listScope, showHidden, dateBounds(), creatorIds, focus, q, sources, stateFilter),
      )
      if (gen !== countsFetchGenRef.current) return // superseded by a newer fetch — discard
      setTagCounts(data)
    } catch {
      // Keep the last-known totals.
    }
  }

  // R5: the live worktree set, fire-and-forget (failure keeps the prior set — no link, never a broken page).
  // Skipped without `worktrees:view`, else one refused request per poll cycle forever.
  async function fetchWorktrees() {
    if (!canReadWorktrees) return
    try {
      const data = await listWorktrees()
      const next = Array.isArray(data) ? data : []
      /* An unchanged pool keeps its identity: the row actions' worktree joins key on it, and every
         `task` event re-reads it. A few dozen slots — the comparison costs less than one row's render. */
      setWorktrees(prev => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next))
    } catch {
      // Keep the last-known set.
    }
  }

  /* THE CREATOR ROSTER. Refetched on what changes WHO HAS ROWS (project scope, Show hidden) and NOT on the period
     or the pick — the picker must keep offering the people it filters away (`buildTaskCreatorsUrl` is the single
     home for the roster URL's params). Declared above the list fetch so the cheap request fires first.
     RECONCILES THE RESTORED PICK ONLY ON SUCCESS: an id with no rows in the new scope is an invisible narrowing,
     but a FAILED fetch is not that answer (a transient 500 must not widen a chosen filter) — as the tag catalog.
     KEYED ON THE ADMIN TOO: the roster always carries the caller's own row, so an in-place admin switch must refetch. */
  useEffect(() => {
    let cancelled = false
    void get<TaskCreator[]>(buildTaskCreatorsUrl(listScope, showHidden))
      .then(roster => {
        if (cancelled) return
        const known = Array.isArray(roster) ? roster : []
        setCreators(known)
        const ids = new Set(known.map(creator => creator.id))
        /* A CORRECTION: never pushed (a response, not the operator, drove it) and never written when there is
           nothing to correct (an equal write is still a router commit, and this runs on every scope change). */
        const base = liveParamsRef.current
        const current = readTaskUrlFilters(base)
        const kept = current.creatorIds.filter(id => ids.has(id))
        if (kept.length !== current.creatorIds.length) {
          const written = writeTaskUrlFilters({ ...current, creatorIds: kept }, base)
          liveParamsRef.current = written
          setSearchParams(written, { replace: true })
        }
      })
      .catch(() => {
        // The board never depends on the roster: this visit loses the picker, keeps the filter.
      })
    return () => { cancelled = true }
  }, [listScopeKey, showHidden, admin?.id])

  /* THE SOURCE VOCABULARY, once: it is fixed per deploy. Reconciled as the creator pick is, and only on success:
     a code the server does not know is an invisible narrowing with no word for its chip. */
  useEffect(() => {
    let cancelled = false
    void readTaskSources()
      .then(data => {
        if (cancelled) return
        const known = Array.isArray(data) ? data : []
        setTaskSources(known)
        const codes = new Set(known.map(source => source.value))
        const base = liveParamsRef.current
        const current = readTaskUrlFilters(base)
        const kept = current.sources.filter(code => codes.has(code))
        if (kept.length !== current.sources.length) {
          const written = writeTaskUrlFilters({ ...current, sources: kept }, base)
          liveParamsRef.current = written
          setSearchParams(written, { replace: true })
        }
      })
      .catch(() => {
        // The board never depends on the vocabulary: this visit loses the picker, keeps the filter.
      })
    return () => { cancelled = true }
  }, [])

  // Initial load + refetch whenever a filter or the lazy window changes. `limit` is a dep (growing the window
  // IS a refetch), and `patchUrlFilters` resets it in the same batched render, so one request per change.
  // `creatorKey`, not `creatorIds` (see its declaration). `zone` because it is half of what the date window
  // MEANS: a changed or late-answering timezone setting changes the request.
  // The window's shape too (`windowShape`): Group by State on or off, Done folded or not, is another row set.
  useEffect(() => {
    if (appendedLimitRef.current === limit) {
      appendedLimitRef.current = null
      return
    }
    appendedLimitRef.current = null
    void fetchTasks(true)
  }, [
    listScopeKey, showHidden, dateRange, dateFrom, dateTo, zone, dayStart, creatorKey, sourceKey, focus, limit, q,
    stateFilter, windowShape.sectionOrder, windowShape.excludeDone,
  ])

  /* KEYED ON THE ADMIN: a tag group is a reference a principal may not be able to read, so an in-place admin switch
     must not keep the previous vocabulary or reconcile against it (as `fetchWorktrees` with `canReadWorktrees`). */
  useEffect(() => {
    void fetchTagGroups()
  }, [admin?.id])

  // The counts answer to the list's filters plus `groupBy` (which turns the fetch on for TAG groups). NOT
  // `limit` (the window never changes what is counted). `zone` as for the list — else a total on yesterday's
  // calendar sits beside today's rows.
  useEffect(() => {
    void fetchTagCounts()
  }, [effectiveGroupBy, listScopeKey, showHidden, dateRange, dateFrom, dateTo, zone, dayStart, creatorKey, sourceKey, focus, q, stateFilter])

  /* THE SELECTION MOVES — a create's landing. A push (Back walks it); the filters, grouping and
     view ride along untouched. */
  function navigateScope(next: TaskScope) {
    const params = new URLSearchParams(liveParamsRef.current)
    params.set(SCOPE_PARAM, scopeKey(next))
    writeUrl(params)
  }

  /* CLEAR ALL — every badge-counted filter plus Search, the URL's in one push (so Back undoes them in one step).
     NOT Focus: it has no chip and no badge count, so clearing it was a change nothing on screen announced; its own
     toggle turns it off. */
  function clearFilters() {
    if (hasTaskUrlFilters(urlFilters)) patchUrlFilters({ showHidden: false, dateRange: 'all', creatorIds: [], sources: [], state: '', q: '' })
  }

  function changeShowHidden(next: boolean) {
    patchUrlFilters({ showHidden: next })
  }

  /* Focus resets the lazy window like every filter — turning it ON over a 500-row window would page 500 rows to
     draw a handful. */
  function changeFocus(next: boolean) {
    setFocus(next)
    setLimit(TASK_PAGE_SIZE)
  }

  /* The period's setters; the window reset is `patchUrlFilters`'s. Picking `exact` KEEPS the endpoints; an `exact`
     with none resolves to no bounds (`lib/taskDateRange.ts`), never an empty board. */
  function changeDateRange(next: TaskDateRangeId) {
    patchUrlFilters({ dateRange: next })
  }

  function changeDateFrom(next: string) {
    patchUrlFilters({ dateFrom: next })
  }

  function changeDateTo(next: string) {
    patchUrlFilters({ dateTo: next })
  }

  /* THE ROSTER, ORDERED ONCE — current user first (`lib/taskCreators.ts`) — for both surfaces, so the eight-cap
     strip and the panel never disagree on who shows. */
  const orderedCreators = useMemo(
    () => orderCreators(creators, admin?.id ?? null),
    [creators, admin?.id],
  )

  /* One setter for both surfaces, so neither holds a pick the other cannot see. */
  function changeCreatorIds(next: string[]) {
    patchUrlFilters({ creatorIds: next })
  }

  function changeSources(next: string[]) {
    patchUrlFilters({ sources: next })
  }

  function changeStateFilter(next: string) {
    patchUrlFilters({ state: next })
  }

  /* THE SEARCH TERM, committed once per typing pause by the header's box (`pane/TaskSearchBox.tsx`). STORED
     VERBATIM; `writeTaskUrlFilters` and `buildTasksUrl` each decline a whitespace-only term. */
  function changeQ(next: string) {
    patchUrlFilters({ q: next }, { replace: true })
  }

  /* Removing the search chip is a navigation, not typing: it pushes. */
  function clearQ() {
    patchUrlFilters({ q: '' })
  }

  /* Grouping does NOT reset the lazy window: it changes no row set (same rows, sectioned), so a reset would throw
     away scrolled pages for nothing. */
  function changeGroupBy(next: string) {
    const params = new URLSearchParams(liveParamsRef.current)
    const value = groupParamValue(next)
    if (value === null) params.delete(GROUP_PARAM)
    else params.set(GROUP_PARAM, value)
    writeUrl(params, { resetWindow: false })
  }

  /* A SECTION's fold. Only EXPLICIT choices are stored, so an untouched section stays open (as `phaseOpen`). */
  /* THE COLLAPSE KEY, NAMESPACED BY AXIS: `''` is both axes' fallback (Untagged / No project), so a flat keyspace
     would fold No project when Untagged was folded — and No project holds rows no other section shows
     (`components/tasks/taskTree.ts`). Real sections are uuids. Tag groups still share one Untagged fold. The
     HOST's key only — renderers pass the bare id, so handles and e2e locators are unchanged. */
  const collapseKey = (sectionId: string) =>
    `${isProjectGroupBy(effectiveGroupBy) ? 'project' : 'tag'}:${sectionId}`

  function toggleSection(sectionId: string, collapsed: boolean) {
    setSectionCollapsed(prev => ({ ...prev, [collapseKey(sectionId)]: !collapsed }))
  }

  /* UNFOLD THE SECTION THE NEW ROW LANDS IN, so an inline create is never an apparent no-op. Tag axis: always `''`
     Untagged (a new task has no tags). Project axis: the created row's OWN `project_id`, read off the server's
     answer, never re-derived from the create ladder. One statement, called by both create rows. */
  function unfoldSectionFor(created: PipelineTask) {
    const landing = collapseKey(isProjectGroupBy(effectiveGroupBy) ? String(created.project_id) : '')
    setSectionCollapsed(prev => (prev[landing] ? { ...prev, [landing]: false } : prev))
  }

  // R5: load worktrees on mount, keyed on the capability too: the shell renders from the persisted permission
  // snapshot, so `worktrees:view` may arrive late.
  useEffect(() => {
    void fetchWorktrees()
  }, [canReadWorktrees])

  // RESTORE the saved outline on mount and whenever the admin changes (`setExpanded({})` drops the previous
  // admin's rows). Only IDS are stored, so each is REFETCHED (never yesterday's snapshot as current) — NOT via
  // `refetchExpandedDetails`, whose `id in prev` guard would discard every restored id. A failing id is stale and
  // dropped silently (no spinner left).
  // ABOVE the deep-link effect, so the reset lands before `revealTask` expands its ancestor.
  // Both gates are armed from a PROMISE CONTINUATION, never inline: the write-through effects run in this flush
  // with OLD values and would save an empty map (or the previous admin's ids under the new key).
  useEffect(() => {
    let cancelled = false
    setExpanded({})
    const ids = loadExpandedTaskIds(expandedStorageKey)
    void Promise.all(
      ids.map(async id => {
        try {
          const detail = await get<PipelineTask>(`/api/tasks/${id}`)
          // Merged, not assigned: a deep link or a quick click may have expanded a row meanwhile.
          if (!cancelled) setExpanded(prev => ({ ...prev, [id]: detail }))
        } catch {
          // A non-resolving id is forgotten; the write-through prunes it on the next expand/collapse.
        }
      }),
    ).then(() => {
      // Armed only once the WHOLE batch settled, or a mid-batch write would persist a subset.
      if (!cancelled) restoredExpandedKeyRef.current = expandedStorageKey
    })
    return () => { cancelled = true }
  }, [expandedStorageKey])

  // WRITE THROUGH on changes to the expanded ID SET (ascending, stable `Object.keys`), so detail refetches
  // rewriting VALUES cost no storage write. The save's result is ignored: failing to remember must never cost the
  // expansion (`saveExpandedTaskIds` swallows blocked-storage throws).
  const expandedIdsKey = Object.keys(expanded).join(',')
  useEffect(() => {
    if (restoredExpandedKeyRef.current !== expandedStorageKey) return
    saveExpandedTaskIds(expandedStorageKey, Object.keys(expanded).map(Number))
  }, [expandedIdsKey, expandedStorageKey])

  // RE-restore the stored Focus when the bucket changes (a late admin id, or an in-place admin switch). A
  // NO-OP on mount (the ref holds the mount key), so the first fetch is the only fetch.
  // …AND IT CLEARS THE URL FILTERS: they are keyed on nobody, so the incoming operator would inherit the outgoing
  // one's creator picks. Guarded, since an unconditional write is a router commit on every snapshot→server
  // handoff. `limit` resets (a different row set).
  // Armed from a PROMISE CONTINUATION, as the outline's restore: inline, the write-through in this flush would
  // save the OUTGOING admin's values under the INCOMING key.
  useEffect(() => {
    if (restoredFiltersKeyRef.current === filtersStorageKey) return
    const restored = loadTaskFilters(filtersStorageKey)
    if (hasTaskUrlFilters(urlFilters)) {
      const cleared = clearTaskUrlFilters(liveParamsRef.current)
      liveParamsRef.current = cleared
      setSearchParams(cleared, { replace: true })
    }
    // An unreplaced `focus` would be written through as the OUTGOING value into the INCOMING bucket — also on
    // the ordinary late-id path.
    setFocus(restored.focus)
    setLimit(TASK_PAGE_SIZE)
    let cancelled = false
    void Promise.resolve().then(() => {
      if (!cancelled) restoredFiltersKeyRef.current = filtersStorageKey
    })
    return () => { cancelled = true }
  }, [filtersStorageKey])

  // …and WRITE THROUGH the stored Focus. The result is ignored (failing to remember must never cost the
  // filtering; `saveTaskFilters` swallows blocked-storage throws).
  useEffect(() => {
    if (restoredFiltersKeyRef.current !== filtersStorageKey) return
    saveTaskFilters(filtersStorageKey, { focus })
  }, [filtersStorageKey, focus])

  // Live updates on `task` SSE events (every pipeline transition emits `emit_console_event(entity="task")`).
  // R5: a `task` burst refetches at once, then at most once per `TASKS_LIVE_REFETCH_MIN_MS` with the burst's
  // tail always read (mount and manual Refresh stay immediate); R6 guards discard out-of-order responses. R9: the
  // same refetch is the stream-`down` poll.
  useLiveRefetch(useEventStream(), () => {
    void fetchTasks()
    void refetchExpandedDetails()
    void fetchWorktrees()
    // Section totals ride the same events: a header must never disagree with its own rows.
    void fetchTagCounts()
    refetchFeedStateCounts()
  }, STREAM_DOWN_POLL_MS, { entity: 'task', minIntervalMs: TASKS_LIVE_REFETCH_MIN_MS })
  /* A running task's `metered` usage (the Tokens/Cost cells) re-reads only the rows, at most once a minute: one
     event per runner every few minutes would otherwise re-read the whole board each time. The figures already
     trail by minutes, so the throttle costs nothing a reader could see. */
  useLiveRefetch(useEventStream(), () => {
    void fetchTasks()
    void refetchExpandedDetails()
  }, null, { entity: 'task', actions: ['metered'], minIntervalMs: METERED_REFETCH_MIN_MS })

  async function toggleExpand(id: number) {
    if (id in expanded) {
      setExpanded(prev => {
        const next = { ...prev }
        delete next[id]
        return next
      })
      return
    }
    setFetching(prev => withAdded(prev, id))
    try {
      const detail = await get<PipelineTask>(`/api/tasks/${id}`)
      setExpanded(prev => ({ ...prev, [id]: detail }))
    } catch {
      setExpanded(prev => ({ ...prev, [id]: { error: 'Failed to load task detail' } }))
    } finally {
      setFetching(prev => withRemoved(prev, id))
    }
  }

  // ── BA R9: the Title → task deep link ────────────────────────────────────────────────────────────
  // Resolve `?task=<id>` into a REVEALED row: clear the project filter, fetch the target, walk
  // `parent_task_id` to its top-level ancestor, expand it so the child mounts, and pin the ancestor when outside
  // the bounded list. Scrolling is the DOM-keyed effect below.
  // `keptScope`: the selection a link carried and the consumer KEPT. A target outside it is not revealed —
  // no selection left waiting for a row the board will not show.
  async function revealTask(targetId: number, keptScope: TaskScope | null) {
    setDeepLinkError('')
    setDeepLinkedId(targetId)
    // AC9.2: never land on a list that hides the linked row — the consumer below has already moved `?scope=`
    // (All projects, or the project the link named) in the same pass that strips the param.
    setLimit(TASK_PAGE_SIZE)
    let target: PipelineTask
    try {
      target = await get<PipelineTask>(`/api/tasks/${targetId}`)
    } catch {
      // A deleted task or dangling `worktrees.task_id` — say so; clear the highlight so a later row sharing the
      // id is not marked.
      setDeepLinkedId(null)
      setDeepLinkError(`Task ${targetId} no longer exists — it may have been deleted.`)
      return
    }
    // `seen` guards `parent_task_id` cycles (no DB constraint forbids them); an unfetchable parent stops the walk
    // at the deepest resolved ancestor.
    let ancestor = target
    const seen = new Set<number>([ancestor.id])
    while (ancestor.parent_task_id !== null && !seen.has(ancestor.parent_task_id)) {
      let parent: PipelineTask
      try {
        parent = await get<PipelineTask>(`/api/tasks/${ancestor.parent_task_id}`)
      } catch {
        break
      }
      seen.add(parent.id)
      ancestor = parent
    }
    // `GET /api/tasks/{id}` carries `children`, so expanding is a state write, not a second request. A top-level
    // target needs no expand.
    if (ancestor.id !== target.id) {
      setExpanded(prev => ({ ...prev, [ancestor.id]: ancestor }))
    }
    if (keptScope !== null && !taskInScope(keptScope, ancestor)) {
      setDeepLinkedId(null)
      return
    }
    // Merged only when the bounded page lacks it (see `displayTasks`).
    setPinnedTask(ancestor)
  }

  // Consume the param and STRIP it (`replace: true`) in the same pass, or every reload re-runs the reveal and
  // re-clears the filter. The page's one `useSearchParams()` is declared with the URL filters above.
  const deepLinkParam = searchParams.get(TASK_DEEP_LINK_PARAM)
  // The optional scope (`?project=`), stripped in the SAME pass — left on, it would be a silent second filter.
  const deepLinkProjectParam = searchParams.get(TASK_DEEP_LINK_PROJECT_PARAM)
  useEffect(() => {
    if (deepLinkParam === null) return
    const targetId = parseTaskDeepLinkParam(deepLinkParam)
    const linkedProjectId = parseTaskDeepLinkProjectParam(deepLinkProjectParam)
    const keptScopeRaw = searchParams.get(SCOPE_PARAM)
    setSearchParams(
      prev => {
        const next = new URLSearchParams(prev)
        next.delete(TASK_DEEP_LINK_PARAM)
        next.delete(TASK_DEEP_LINK_PROJECT_PARAM)
        // The reveal moves the selection: a named project becomes it, an unnamed link widens to All projects,
        // spelled `all` (for a real target only — junk moves nothing). A link that CARRIES `?scope=` (the task
        // editor's return) keeps it: the operator's selection stays put even if the row is outside it.
        if (targetId !== null && !prev.has(SCOPE_PARAM)) {
          next.set(
            SCOPE_PARAM,
            scopeKey(linkedProjectId === null ? ALL_SCOPE : { kind: 'project', projectId: linkedProjectId }),
          )
        }
        return next
      },
      { replace: true },
    )
    // A malformed param is a no-op, never a `GET /api/tasks/NaN`.
    if (targetId !== null) void revealTask(targetId, keptScopeRaw === null ? null : parseScope(keptScopeRaw))
  }, [deepLinkParam, deepLinkProjectParam])

  /* A rail feature's "+" lands here asking for the inline composer (a stamp, so a second "+" asks again). The
     row may mount a frame after the navigation (`focusComposerSoon`). */
  const location = useLocation()
  const focusComposerStamp = (location.state as Record<string, unknown> | null)?.[FOCUS_COMPOSER_STATE]
  useEffect(() => {
    if (focusComposerStamp === undefined) return
    // Consumed once: history state survives a reload, which must not re-grab focus.
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
    return focusComposerSoon()
  }, [focusComposerStamp])

  /* AN EMPTY FEATURE OPENS ON ITS COMPOSER (AC-5.5): "No tasks yet" says what is missing, the focused input is
     the one action. Once per feature selected. */
  const emptyFeatureId =
    scope.kind === 'feature' && scopedFeatureLocation.feature?.counts.total === 0 ? scope.featureId : null
  useEffect(() => {
    if (emptyFeatureId === null) return
    // Never pulled out of the rail while the operator is walking it with the keyboard.
    if (document.activeElement?.closest('[role="tree"]')) return
    return focusComposerSoon()
  }, [emptyFeatureId])

  // …and PUBLISH the scope for the rail's highlight; `null` once no board is mounted.
  const currentScopeKey = scopeKey(scope)
  useEffect(() => { publishBoardScope(currentScopeKey) }, [currentScopeKey])
  useEffect(() => () => publishBoardScope(null), [])

  // Scroll when THE ROW EXISTS IN THE DOM, observed on the DOM itself: the ancestor's expand replaces the
  // subtree, possibly behind a `Suspense` boundary that re-renders without this container. A MutationObserver
  // fires on the node's arrival and disconnects on first success. `block: 'center'` keeps the row out from under
  // the sticky header.
  useEffect(() => {
    if (deepLinkedId === null) return
    const rowId = taskRowElementId(deepLinkedId)
    function scrollToRow(): boolean {
      const row = document.getElementById(rowId)
      if (!row) return false
      row.scrollIntoView({ block: 'center', behavior: 'smooth' })
      return true
    }
    if (scrollToRow()) return
    const observer = new MutationObserver(() => {
      if (scrollToRow()) observer.disconnect()
    })
    observer.observe(document.body, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [deepLinkedId])

  // The revealed row stays selected until the operator's NEXT INTERACTION (pointer or key), not a timed flash.
  useEffect(() => {
    if (deepLinkedId === null) return
    const clear = () => setDeepLinkedId(null)
    document.addEventListener('pointerdown', clear, { capture: true, once: true })
    document.addEventListener('keydown', clear, { capture: true, once: true })
    return () => {
      document.removeEventListener('pointerdown', clear, { capture: true })
      document.removeEventListener('keydown', clear, { capture: true })
    }
  }, [deepLinkedId])

  // AC1c.2/AC1c.3: Edit NAVIGATES to the task page, which fetches the full task (incl. `body`) itself — so no
  // pre-fetch guard is needed against saving an empty Description.

  /* A MUTATION RESPONSE PRICES NOTHING, SO THE ROW KEEPS THE COST IT ALREADY HAD. Only list and detail routes
     resolve a cost; absent-or-`null` `cost_status` means "no claim about cost" (`types.ts` is the single home), so
     the last resolved figure remains the best known — blanking it would disagree with the Details tab.
     A RESOLVED `'unavailable'` IS A CLAIM and overwrites. The rating fields share this latent bug, left alone.
     The token total rides the same rule: a payload that resolved neither carries the last known one. */
  function carryCost(updated: PipelineTask, previous: PipelineTask): PipelineTask {
    if (updated.cost_status != null) return updated
    return {
      ...updated,
      cost_usd: previous.cost_usd,
      cost_status: previous.cost_status,
      cost_agent_reported: previous.cost_agent_reported,
      tokens_total: updated.tokens_total ?? previous.tokens_total,
    }
  }

  // Replace an updated Task wherever it lives (top-level row and/or cached detail, as parent or child), so a
  // mutation updates the UI with no refetch. Responses without `children` keep the cached children.
  function replaceTaskEverywhere(updated: PipelineTask) {
    setTasks(prev => prev.map(t => (t.id === updated.id ? { ...carryCost(updated, t), children: t.children } : t)))
    setExpanded(prev => {
      const next = { ...prev }
      for (const key of Object.keys(next).map(Number)) {
        const entry = next[key]
        if ('error' in entry) continue
        if (entry.id === updated.id) {
          next[key] = { ...carryCost(updated, entry), children: entry.children }
        } else if (entry.children) {
          next[key] = {
            ...entry,
            children: entry.children.map(c =>
              c.id === updated.id ? { ...carryCost(updated, c), children: c.children } : c,
            ),
          }
        }
      }
      return next
    })
  }

  // A2/AC2.6 + AD-14: the expanded region is phase and subtask rows ONLY; the gate form and version history live
  // on the standalone task page (`/tasks/:id`). Cost: answering an `awaiting_response` subtask takes one extra click.

  /* THE FIVE ROW VERBS (`useTaskActions`, shared with the task page) under this board's busy/error model
     (`rowWrite`). Start/Reset replace the row in place; Stop/Complete/force-start REFETCH — the row moves ACROSS
     the board's halves in the server's order, and patched in place it would sit above the gap indefinitely on a
     quiet stream. Delete refetches too. The dialog echoes the clicked verb. */
  const {
    start: startTask, stop: stopTask, reset: resetTask, complete: completeTask, confirmDelete: deleteTask,
    askPushIdentity, preemptDialog,
  } = useTaskActions({
    confirm,
    run: rowWrite,
    isBusy: task => submitting.has(task.id),
    withBusy: (task, fn) => withBusy(task.id, fn),
    onRow: replaceTaskEverywhere,
    onMoved: fetchTasks,
    deleteDialog: (task, verb) => ({
      title: verb,
      message: (
        <>
          {verb} “<strong>{displayTitle(task.title, { showPersona: true })}</strong>”?{LIVE_STATES.has(task.state) && <> It will be stopped.</>} This cannot be undone.
        </>
      ),
      failure: 'Failed to delete task',
    }),
    onDeleted: fetchTasks,
  })

  /* A PRIORITY BUMP THAT SORTS AHEAD OF RUNNING WORK (the deleted Queue style's): the row is replaced in
     place, then — while it is still queued — the forecast is asked, and a capacity-blocked bump opens `PreemptDialog`
     (never on a pending org, whose force-start the API refuses too). No client-side "sorts ahead of a running row"
     pre-check: the Queue read the whole running set, but the board holds only its page and filters, so the forecast
     (null when no preempt applies) decides. "Just queue it" leaves it queued: the PATCH already moved it. A failed
     forecast is a null. */
  async function priorityChanged(updated: PipelineTask) {
    replaceTaskEverywhere(updated)
    if (updated.state !== 'ready' || awaitingApproval) return
    const forecast = await preemptForecast(updated.id)
    if (forecast) setPriorityPreempt({ task: updated, forecast })
  }

  // An acting operator is asked whom to push as first, as every Start door asks; Cancel posts nothing.
  async function priorityForceStart(task: PipelineTask, stopTaskId: number) {
    setPriorityPreempt(null)
    const pushAs = await askPushIdentity({ projectId: task.project_id, title: task.title, assigneeId: task.assigned_to_id })
    if (!pushAs) return
    void rowWrite(task, 'Failed to force-start task', async () => {
      const body: ForceStartBody = {
        stop_task_id: stopTaskId,
        ...startBody(startIntentFor(task, task.parent_task_id === null ? 'root' : 'leaf'), pushAs),
      }
      await post<PipelineTask>(DISPATCH_ROUTES.forceStart(task.id), body)
      await fetchTasks()
    })
  }

  /* DUPLICATE is a NAVIGATION, not a write — no `submitting`, errors or refetch here. What a copy carries:
     `lib/taskDuplicate.ts`. */
  // R5: DELIVERY project id→name (a Task's `project_id`, see `allProjects`), to match the worktree roster.
  // Memoized; the joins run over a few dozen rows (`lib/worktree.ts`), not worth an index.
  const projectNameById = useMemo(() => {
    const map = new Map<number, string>()
    for (const p of allProjects) map.set(p.id, p.name)
    return map
  }, [allProjects])

  // R5: the slot this Task's 🌐 STACK LINK points at — the join is `lib/worktree.ts::worktreeForTask` (its single
  // home); the predicate requires a stack up and serving `urls.web`.
  // Kept per (roster, pool), so a row redraws its links when either moves and only then.
  const worktreeFor = useCallback(
    (task: PipelineTask): Worktree | undefined =>
      worktreeForTask(task.id, projectNameById.get(task.project_id), worktrees,
        wt => wt.stack_state === 'up' && !!wt.urls?.['web']),
    [projectNameById, worktrees],
  )

  // The slot whose CONSOLE this row can open: the same join under a live-session test (`lib/worktree.ts`).
  const consoleWorktreeFor = useCallback(
    (task: PipelineTask): Worktree | undefined =>
      runnerWorktreeFor(task.id, projectNameById.get(task.project_id), worktrees),
    [projectNameById, worktrees],
  )

  // Opens the dock console on a slot; a row passes only one already gated (`rowActions.tsx`).
  function openConsole(wt: Worktree) {
    openDockConsole({
      project: wt.project,
      projectId: wt.project_id,
      number: wt.number,
      nodeId: wt.node_uuid,
      // Same server verdict and fail-closed default as `components/WorktreePool.tsx`'s.
      mayEdit: wt.may_edit,
      // The opener's title snapshot; the dock refines it from the live `['worktrees']` cache.
      title: resolveTitle(wt),
      // Whether the row showed a session when clicked: this board gates the console on the TASK's state, so a dead
      // runner's task still offers it; the pane uses this to say "empty slot" rather than a glitch-like message
      // (`ConsolePane.tsx`'s SLOT_EMPTY_REASON).
      hadLiveSession: hasLiveSession(wt),
    })
  }

  // BA R9: the rendered rows — the loaded window plus the deep link's top-level ancestor pinned in front WHEN THE
  // WINDOW LACKS IT (an untouched epic is routinely off the most-recently-updated window). The pin dissolves once a
  // refetch or scroll brings the row in.
  // …AND ONLY WHILE THE BOARD'S SCOPE CAN CONTAIN IT: the pin outlives the reveal, and after a scope switch it would
  // plant another project's task on this board. Switching back re-merges it.
  const pinnedIsMerged =
    pinnedTask !== null &&
    taskInScope(scope, pinnedTask) &&
    !tasks.some(t => t.id === pinnedTask.id)
  const displayTasks = useMemo(
    () => (pinnedIsMerged && pinnedTask ? [pinnedTask, ...tasks] : tasks),
    [tasks, pinnedTask, pinnedIsMerged],
  )
  /* Another org's rows on the board (an operator's cross-org view) — every row drawn, the pinned one and an
     expanded epic's stage tasks included: each row's verbs — Start, Stop, Reset, Complete, Priority…, Waiting… — go as
     its org for the one request (`lib/actAs.ts::useActAsRows`). */
  const heldRows: { path: string; org: OrgRef | null }[] = []
  const holdRows = (rows: readonly PipelineTask[] | null | undefined) => {
    for (const row of rows ?? []) {
      const org = otherOrgOf(row.org)
      if (org) heldRows.push({ path: `/api/tasks/${row.id}`, org })
      holdRows(row.children)
    }
  }
  holdRows(displayTasks)
  holdRows(Object.values(expanded).filter((entry): entry is PipelineTask => !('error' in entry)))
  useActAsRows(heldRows)

  // ── Lazy load on scroll ─────────────────────────────────────────
  // More rows exist while the last answer carried a cursor, and never past the server's ceiling (a refetch of the
  // whole window is one `?limit=` request, clamped there).
  const canLoadMore = !loading && !loadingMore && nextCursor !== null && limit < TASK_LIST_MAX

  /* ONE PAGE PAST THE CURSOR, appended — never the whole window again. Shares the list's generation: a refetch
     started meanwhile wins (it asks the window this page would have grown), and this page landing discards an
     older refetch that would drop it. A row already held (its key moved between reads) is not repeated. */
  async function loadNextPage() {
    if (nextCursor === null) return
    const gen = ++listFetchGenRef.current
    const epoch = boardRowsEpoch()
    const size = Math.min(TASK_PAGE_SIZE, TASK_LIST_MAX - limit)
    latestListUrlRef.current = null
    setLoadingMore(true)
    try {
      const { data, next } = await listPage(`${listUrl(size)}&after=${encodeURIComponent(nextCursor)}`)
      if (gen !== listFetchGenRef.current) return
      // The board now draws the grown window (the `limit` below), an answer older reads must not replace.
      paintedListUrlRef.current = listUrl(limit + size)
      setTasks(prev => {
        const held = new Set(prev.map(t => t.id))
        return [...prev, ...data.filter(t => !held.has(t.id))]
      })
      rememberBoardRows(data, epoch)
      setNextCursor(next)
      appendedLimitRef.current = limit + size
      setLimit(limit + size)
    } catch {
      // The sentinel re-arms: the next intersection asks again.
    } finally {
      setLoadingMore(false)
    }
  }
  // The observer below is armed once per (canLoadMore, limit); it calls the LATEST render's loader.
  const loadNextPageRef = useRef(loadNextPage)
  loadNextPageRef.current = loadNextPage
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  // The list's scroll region (`LIST_SCROLL_SX`): the rows' scrollport above the phone, a plain box on it.
  const listScrollRef = useRef<HTMLDivElement | null>(null)

  // An IntersectionObserver on a sentinel BELOW the list, not a scroll handler: the scrollport is the shell's
  // (a different element at ≤480px). Re-armed on every (canLoadMore, limit) change, so a viewport tall enough for
  // two pages loads both without waiting for a scroll.
  useEffect(() => {
    if (!canLoadMore) return
    const node = sentinelRef.current
    if (!node) return
    const observer = new IntersectionObserver(
      entries => {
        if (!entries.some(entry => entry.isIntersecting)) return
        void loadNextPageRef.current()
      },
        // A page of runway, so scrolling never stops dead at the last row.
      // Rooted at the rows' own scrollport (the list region above the phone, `.app-main` on it): the runway must be
      // measured there, since a nested scroller clips the sentinel before the viewport's margin could see it.
      { root: scrollingAncestor(node), rootMargin: '400px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [canLoadMore, limit])

  /* What the flattener needs to SECTION the board, or `undefined` for None (rows byte-identical to the ungrouped
     board). `undefined` also covers a selection the catalog cannot match yet — a half-grouped board is worse than
     an ungrouped one (`TaskTreeGrouping` carries the trade). */
  const grouping = useMemo<TaskTreeGrouping | undefined>(() => {
    /* THE PROJECT AXIS — one section per project (the repo) from the task tree. AN UNANSWERED TREE FALLS BACK TO
       NO GROUPING (as an unanswered tag catalog): one "No project" heading would misplace every row. No `counts`
       or `untagged` here (argued in `../components/tasks/taskTree`). */
    if (isProjectGroupBy(effectiveGroupBy)) {
      if (!taskTree || taskTree.projects.length === 0) return undefined
      return {
        axis: 'project',
        // The server's order, never re-sorted (as the tag arm).
        projects: taskTree.projects.map(p => ({ id: String(p.id), name: p.name })),
        isCollapsed: (sectionId: string) => sectionCollapsed[collapseKey(sectionId)] === true,
      }
    }
    if (!isTagGroupBy(effectiveGroupBy)) return undefined
    const group = tagGroups.find(g => g.id === effectiveGroupBy && g.state === 'active')
    if (group === undefined) return undefined
    return {
      axis: 'tag',
      // Already in the operator's `position` order; never re-sorted. `?? []` because only the payload's TOP level
      // is shape-checked: a null `tags` degrades to one Untagged section, never a white screen.
      tags: (group.tags ?? []).map(tag => ({ id: tag.id, name: tag.name })),
      counts: tagCounts?.counts ?? {},
      untagged: tagCounts?.untagged ?? null,
      isCollapsed: (tagId: string) => sectionCollapsed[collapseKey(tagId)] === true,
    }
  }, [effectiveGroupBy, tagGroups, tagCounts, sectionCollapsed, taskTree])

  // The board's filters as one memoized value: the State counts' query keys on it.
  const paneFilters = useMemo<PaneFilters>(
    () => ({ showHidden, bounds: dateBounds(), creatorIds, sources, state: stateFilter, focus, q, muted }),
    [showHidden, dateRange, dateFrom, dateTo, zone, dayStart, creatorKey, sourceKey, stateFilter, focus, q, muted],
  )

  /* GROUP BY STATE: the section totals are `GET /api/tasks/counts` (so a header agrees with the rail — AC-6.5) and
     Done folds on `task_done_expanded`. Fetched only while the board sections by State (`feedStateGrouped`). */
  const feedCountsUrl = paneCountsUrl(scope, paneFilters)
  /* THE SAME COUNTS NUMBER SELECT ALL BESIDE THE CHIPS — never the 500-row read Select all presses, just
     to label it. Only where the total IS the pickable count: every row a State Draft board matches is a top-level
     draft (the counts and the list narrow on the row's own state), every row a State Done board matches without
     Show hidden is an unarchived finished row — both pickable wherever the scope's project grants Delete. Anywhere
     else (another state, All projects, a pick that rests on authorship) a total would name rows the press does not
     pick, so the verb goes without a number. The per-row `may_edit` veto is the one residue a total cannot see. */
  const chipCountableState = stateFilter === 'draft' || (stateFilter === 'done' && !showHidden)
  const chipCountable = chipCountableState && !viewer && boardProjectId !== null &&
    canDeleteRow({ project_id: boardProjectId, authored_draft: false, may_edit: true })
  const feedStateCounts = useQuery({
    queryKey: [...PANE_QUERY_KEY, 'counts', feedCountsUrl],
    queryFn: () => get<TaskTreeCounts>(feedCountsUrl),
    enabled: feedStateGrouped || chipCountable,
    staleTime: 10_000,
  })
  const stateGrouping = useMemo<TaskListStateGrouping | undefined>(
    () =>
      feedStateGrouped
        ? {
            counts: feedStateCounts.data,
            doneExpanded: doneOpen,
            // No fold while the State filter is Done: a folded Done would hide every row the filter asks for.
            onToggleDone: stateFilter === 'done'
              ? undefined
              : () =>
                  viewer
                    ? setViewerDoneExpanded(!doneExpanded)
                    : void updatePreferences({ task_done_expanded: !doneExpanded }),
          }
        : undefined,
    [feedStateGrouped, feedStateCounts.data, doneExpanded, doneOpen, stateFilter, updatePreferences, viewer],
  )

  /* THE FEED'S TWO BUNDLES KEEP THEIR IDENTITY: rebuilt every render, they handed every row a new
     prop on each poll or `task` event, so no row could skip its render. A handler reads THIS render's closure
     through `latest` (it runs on a press, never during render); only what a row DRAWS from — the gates, the
     worktree joins, the roster and the pick — re-keys the bundle. */
  const latest = useRef({ navigate, startTask, stopTask, resetTask, completeTask, deleteTask, openConsole, unfoldSectionFor, fetchTasks, expandState })
  latest.current = { navigate, startTask, stopTask, resetTask, completeTask, deleteTask, openConsole, unfoldSectionFor, fetchTasks, expandState }

  /* THE CREATE BUNDLE the Activity feed hands its create row (ladder: `resolveCreateProject` in
     `components/tasks/shared.tsx`). Withheld without `tasks:create`. */
  // A refetch in flight is loading, not unavailable: `isError` outlives the failure until it answers.
  const rosterFailed = projectsQuery.isError && !projectsQuery.isFetching
  const listCreate = useMemo<TaskListCreate | undefined>(
    () => canCreateTasks
      ? {
          projects: allProjects,
          rosterAnswered: projectsAnswered,
          rosterFailed,
          pickedProjectId: createPick,
          onPickProject: setCreatePick,
          targetRef: createTargetRef,
          boardProjectId,
          feature: scopedFeature,
          /* THE CHAT GATE, as the toolbar's New Chat. */
          canChat: canCreateChats,
          onCreated: created => {
            latest.current.unfoldSectionFor(created)
            // The tree's counts move too (a project or its "Other" gains a row).
            void queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
            void latest.current.fetchTasks()
          },
          /* THE TYPED TEXT AND THE BOARD'S SCOPE — `expandState` is the one spelling. */
          onExpand: (body, target) => latest.current.navigate('/tasks/new', { state: latest.current.expandState(body, target) }),
        }
      : undefined,
    [canCreateTasks, allProjects, projectsAnswered, rosterFailed, createPick, boardProjectId, scopedFeature, canCreateChats, queryClient],
  )

  /* The row verbs the feed gets — ONE predicate set (the permission gate is the absent handler). Edit and
     Delete also go to a `tasks:create` holder, narrowed per row (own drafts); the lifecycle handlers stay on flat
     `canEditTasks` (the server's route floor) and `canRunRow` decides each ROW. */
  const rowActions = useMemo(() => ({
    onEdit: canEditTasks || canCreateTasks ? (task: PipelineTask) => latest.current.navigate(`/tasks/${task.id}`) : undefined,
    onStart: canEditTasks ? (task: PipelineTask, intent: StartIntent) => void latest.current.startTask(task, intent) : undefined,
    onStop: canEditTasks ? (task: PipelineTask) => void latest.current.stopTask(task) : undefined,
    onReset: canEditTasks ? (task: PipelineTask) => void latest.current.resetTask(task) : undefined,
    onComplete: canEditTasks
      ? (...args: Parameters<typeof completeTask>) => latest.current.completeTask(...args)
      : undefined,
    onDelete: canDeleteTasks || canCreateTasks ? (task: PipelineTask) => void latest.current.deleteTask(task) : undefined,
    onDuplicate: canCreateTasks
      ? (task: PipelineTask) => duplicateTask(task, otherOrgOf(task.org), latest.current.navigate)
      : undefined,
    /* Filing and the waiting reason are `tasks:edit` writes; `canRunRow` narrows each row. */
    onMoveToFeature: canEditTasks ? setFilingTask : undefined,
    onWaitOnCustomer: canEditTasks ? setWaitingTask : undefined,
    canEditRow,
    canDeleteRow,
    canRunRow,
    /* A pending org starts nothing (the API 403s it): every Start renders disabled with the reason. */
    startBlockedReason: awaitingApproval,
    onSetPriority: canEditTasks ? setPriorityTask : undefined,
    /* Another org's row: a read-only note and Act as lead its ⋯; its Duplicate copies into that org. */
    otherOrgOf,
    worktreeFor,
    consoleWorktreeFor,
    onOpenConsole: (wt: Worktree) => latest.current.openConsole(wt),
  }), [
    canEditTasks, canCreateTasks, canDeleteTasks, canEditRow, canDeleteRow, canRunRow, otherOrgOf, awaitingApproval,
    worktreeFor, consoleWorktreeFor,
  ])

  /* ── BULK DELETE OF DRAFTS, BULK ARCHIVE OF FINISHED ROWS ──────────────────────────
     Removing fifty superseded drafts took fifty page loads; putting away fifty shipped rows took fifty more. A row
     is PICKABLE when one bulk verb can act on it and the operator may (the row's own Delete/Archive gate,
     `canDeleteRow`): a top-level DRAFT (Delete) or a top-level row the ⋯ would ARCHIVE (`isArchiveRow`) and that
     is not archived yet. Never a live row, whose delete stops a runner and deserves its own confirm. The two sets
     are disjoint (a draft never archives), so each verb acts only on its own picks and names their count.
     Picked rows are held whole (the report names them), and a pick the board no longer draws is dropped
     (`onDrawn`), so a bulk verb never acts on rows nobody can see.
     A NEW SCOPE OR FILTER KEEPS THE PICKS IT STILL DRAWS: it used to drop the whole pick silently.
     Now the new list's draw prunes it like any other draw, and a draw that drops picks SAYS SO (`pickNote`) —
     the dock may vanish with them, so the note is the only trace of the change. */
  /* THE PICK LIVES IN A STORE, NOT PAGE STATE (`lib/pickStore.ts`): a tick re-renders itself and the
     dock, never the board. The page re-renders when the pick empties or fills (`picking`: every tick's reveal and
     the age slot follow it), and once more when the clicking pauses (`PICK_ECHO_DELAY_MS`, at low priority) so the
     ⋯ menus' Select/Deselect catch up without a burst of clicks ever waiting on a whole-board render. */
  const [pickStore] = useState(() => createPickStore<PipelineTask>())
  const picking = usePicking(pickStore)
  const [, setPickEcho] = useState(0)
  const { trigger: echoPick, cancel: cancelPickEcho } = useDebouncedCallback(
    () => startTransition(() => setPickEcho(n => n + 1)),
    PICK_ECHO_DELAY_MS,
  )
  useEffect(() => {
    const unsubscribe = pickStore.subscribe(echoPick)
    return () => {
      cancelPickEcho()
      unsubscribe()
    }
    // `echoPick`/`cancelPickEcho` read refs; the subscription outlives their identities.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickStore])
  const clearPicked = useCallback(() => clearPicks(pickStore), [pickStore])
  /* What a draw just dropped from the pick, said once (`role="status"`) and gone after a while or the next tick. */
  const [pickNote, setPickNote] = useState<string | null>(null)
  const pickNoteRef = useRef(pickNote)
  pickNoteRef.current = pickNote
  useEffect(() => {
    if (pickNote === null) return
    const timer = setTimeout(() => setPickNote(null), PICK_NOTE_MS)
    return () => clearTimeout(timer)
  }, [pickNote])
  const mayDelete = rowActions.onDelete !== undefined
  const isPickable = useCallback(
    (task: PipelineTask) =>
      task.parent_task_id === null &&
      (task.state === 'draft' || (isArchiveRow(task) && !task.archived_at)) &&
      canDeleteRow(task) !== false,
    [canDeleteRow],
  )
  // Read by `onDrawn`, assigned below once `useBulkRuns` answers.
  const bulkRunningRef = useRef(false)
  const boardSelection = useMemo<PaneSelection | undefined>(
    () =>
      !viewer && mayDelete
        ? {
            store: pickStore,
            picking,
            isSelectable: isPickable,
            onToggle: task => {
              if (pickNoteRef.current !== null) setPickNote(null)
              togglePick(pickStore, task.id, task)
            },
            onDrawn: drawn => {
              const prev = pickStore.get()
              if (prev.size === 0) return
              const fresh = new Map(drawn.map(task => [task.id, task]))
              const next = new Map<number, PipelineTask>()
              for (const id of prev.keys()) {
                const task = fresh.get(id)
                if (task) next.set(id, task)
              }
              if (next.size === prev.size && [...next].every(([id, task]) => prev.get(id) === task)) return
              pickStore.update(() => next)
              /* NOT DURING A BULK RUN: a live refetch drops the rows it is deleting, and those leave the
                 pick by design, not as a surprise worth a note. */
              if (next.size < prev.size && !bulkRunningRef.current) {
                setPickNote(next.size === 0 ? 'Selection cleared' : `Kept ${next.size} of ${countOf(prev.size, 'pick')}`)
              }
            },
          }
        : undefined,
    [viewer, mayDelete, pickStore, picking, isPickable],
  )
  /* NO `onBatchClean`: it drops the WHOLE pick, and a clean Delete must not drop the finished rows still waiting
     for Archive. Each verb instead unpicks the rows it landed (`runBulkVerb`); a refused row stays picked for a
     retry — the batch rule's intent, per row. */
  const { runs: bulkRuns, reports: bulkReports, runBulk, runReason } = useBulkRuns<PipelineTask, BulkVerbId>({
    identity: bulkTaskIdentity,
  })
  bulkRunningRef.current = Object.keys(bulkRuns).length > 0

  /* SELECT ALL MATCHING: every pickable row the board's filters match — State and the selected project
     narrow it to one project's drafts — up to the window's ceiling, not only the rows scrolled into view. Read
     while the dock shows, so its label carries the count; the press loads those rows into the board (the window
     grows to them, so the pick still names only rows on screen — `onDrawn` keeps it so) and picks them. */
  const matchingUrl = listUrl(TASK_LIST_MAX)
  const matchingQuery = useQuery({
    queryKey: [...MATCHING_QUERY_KEY, matchingUrl],
    queryFn: () => getPage<PipelineTask[]>(matchingUrl),
    enabled: !!boardSelection && picking,
    staleTime: 10_000,
  })
  const matchingRows = matchingQuery.data?.data
  const matchingPickable = useMemo(() => (matchingRows ?? []).filter(isPickable), [matchingRows, isPickable])
  async function selectAllMatching() {
    /* READ AFRESH, never the label's cached answer: the press installs these rows as the board, and a stale read
       would bring back a row deleted meanwhile or pick a draft someone just started. The list's generation and
       epoch are taken BEFORE the read, as `fetchTasks` takes them, so a newer list read wins over this one. */
    const gen = ++listFetchGenRef.current
    const epoch = boardRowsEpoch()
    latestListUrlRef.current = null
    abortListReads()
    const { data: page } = await matchingQuery.refetch()
    if (!page || gen !== listFetchGenRef.current) return
    paintedListUrlRef.current = matchingUrl
    setTasks(prev => keepUnchangedRows(prev, page.data))
    rememberBoardRows(page.data, epoch)
    setNextCursor(page.next)
    if (limit !== TASK_LIST_MAX) {
      appendedLimitRef.current = TASK_LIST_MAX
      setLimit(TASK_LIST_MAX)
    }
    pickStore.update(prev => {
      const next = new Map(prev)
      for (const task of page.data) if (isPickable(task)) next.set(task.id, task)
      return next
    })
  }

  /* SELECT ALL BESIDE THE CHIPS: the dock's verb, offered before the first tick wherever a filter narrows
     the board. Its number is the counts' (`chipCountable`), capped as the press is; without one it says no number. */
  const chipTotal = chipCountable ? feedStateCounts.data?.total : undefined
  const chipSelectAll = boardSelection && chipTotal !== 0
    ? {
        label: chipTotal === undefined
          ? 'Select all'
          : chipTotal > TASK_LIST_MAX ? `Select first ${TASK_LIST_MAX}` : `Select all ${chipTotal}`,
        onRun: () => void selectAllMatching(),
      }
    : undefined

  /* ONE CONFIRM, ONE DELETE PER ROW (`DELETE /api/tasks/{id}` is both verbs: a finished row is deleted for good,
     as the row's own Archive deletes it — `useTaskActions.confirmDelete`), one refetch for the batch. No cache keeps a row
     that left the board: `forgetDeletedTasks` invalidates the cached reads, and the window is refetched. */
  async function runBulkVerb(id: BulkVerbId, rows: PipelineTask[], words: Pick<ConfirmOptions, 'title' | 'message' | 'confirmLabel'>, verb: { doing: string; done: string }) {
    /* One confirm may reach another org's rows (each goes as its org, `useDeleteTask`): it names those orgs. */
    const otherOrgs = [...new Set(rows.map(row => otherOrgOf(row.org)?.name).filter((name): name is string => !!name))]
    const message = otherOrgs.length > 0 ? <>{words.message} Includes {otherOrgs.join(', ')}'s tasks.</> : words.message
    if (!(await confirm({ ...words, message, identity: bulkIdentity(rows, bulkTaskIdentity), destructive: true }))) return
    const landed = new Set<number>()
    await runBulk(id, verb, rows, async task => {
      await sendTaskDelete(task)
      landed.add(task.id)
    })
    pickStore.update(prev => new Map([...prev].filter(([taskId]) => !landed.has(taskId))))
    forgetDeletedTasks(queryClient, [...landed])
    void queryClient.invalidateQueries({ queryKey: MATCHING_QUERY_KEY })
    void fetchTasks()
  }

  /* A verb is offered while it has picks to act on — or a run or report still to show (the reports draw per
     offered verb). Its label carries ITS count, not the bar's: a mixed pick says "Delete 2" and "Archive 3". */
  const bulkVerbOffered = (id: BulkVerbId, n: number) => n > 0 || !!bulkRuns[id] || !!bulkReports[id]
  /* THE DOCK HIDES NO ROW IN USE. Sticky, it sits on whichever rows scroll under it, and its arrival
     covered the very row just picked when that row was low in the viewport (row 37 at 1024px). At the list's end
     it reserves its own height (it is in flow). Mid-list, the focused control is lifted clear whenever the dock
     appears or grows (a report) and whenever focus moves under it, so the row being picked or tabbed to stays
     readable. A pointer can never reach a row under the dock (the dock takes the press), so focus is the one way in.
     …AND THE SCROLLPORT RESERVES ITS HEIGHT: while the dock shows, its scrollport's
     `scroll-padding-bottom` is the dock's height, so the browser's own scrolling — Page Down, Space, a row
     scrolled into view, a focus move — ends every row above the dock instead of under it, at any width. */
  const bulkDockShown = !!boardSelection && (picking || Object.keys(bulkRuns).length > 0 || Object.keys(bulkReports).length > 0)
  const bulkDockRef = useRef<HTMLDivElement>(null)
  useLauncherYield(bulkDockRef, bulkDockShown || pickNote !== null)
  useEffect(() => {
    const dock = bulkDockRef.current
    const page = dock?.parentElement
    if (!bulkDockShown || !dock || !page) return
    // The ROWS' scrollport: the list region above the phone (the dock sits under it), `.app-main` on the phone (the
    // one the dock sticks to). Read from inside the region, never from the dock.
    const region = listScrollRef.current
    const pane = region?.firstElementChild instanceof HTMLElement ? scrollingAncestor(region.firstElementChild) : scrollingAncestor(dock)
    const port = pane ?? document.documentElement
    const reservedBefore = port.style.scrollPaddingBottom
    // Only a scrollport the dock floats over reserves its height; the list region ends above the dock.
    const overlaps = port.contains(dock)
    const reserve = () => { if (overlaps) port.style.scrollPaddingBottom = `${dock.offsetHeight}px` }
    const lift = () => {
      reserve()
      const focused = document.activeElement
      if (!(focused instanceof HTMLElement) || !page.contains(focused) || dock.contains(focused)) return
      const hidden = focused.getBoundingClientRect().bottom - dock.getBoundingClientRect().top
      if (hidden <= 0) return
      if (pane) pane.scrollTop += hidden
      else window.scrollBy(0, hidden)
    }
    lift()
    // jsdom has no ResizeObserver; the first lift and the focus listener still run there.
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(lift)
    observer?.observe(dock)
    page.addEventListener('focusin', lift)
    return () => {
      observer?.disconnect()
      page.removeEventListener('focusin', lift)
      port.style.scrollPaddingBottom = reservedBefore
    }
  }, [bulkDockShown])

  /* The dock's verbs for the pick it reads (`TaskBulkDock`), so the count and labels follow each click at once. */
  function bulkActionsFor(pickedTasks: PipelineTask[]): BulkAction[] {
    if (!boardSelection) return []
    const pickedIds = new Set(pickedTasks.map(task => task.id))
    const pickedDrafts = pickedTasks.filter(task => task.state === 'draft')
    const pickedArchivable = pickedTasks.filter(isArchiveRow)
    const unpicked = matchingPickable.filter(task => !pickedIds.has(task.id)).length
    /* "first": the board's ceiling stopped the read with matching rows behind it. */
    const selectAllLabel = matchingQuery.data?.next ? `Select first ${matchingPickable.length}` : `Select all ${matchingPickable.length}`
    return [
        {
          id: SELECT_ALL,
          label: matchingQuery.data ? selectAllLabel : 'Select all',
          /* The ⋯ menu's Select mark, so "☑ Select all 75" reads as picking. */
          icon: '☑',
          disabled: !matchingQuery.data || unpicked === 0,
          disabledReason: matchingQuery.isError
            ? 'Could not count the matching tasks'
            : !matchingQuery.data ? 'Counting matching tasks' : 'Every matching task is picked',
          onRun: () => void selectAllMatching(),
        },
        ...(bulkVerbOffered(BULK_DELETE, pickedDrafts.length)
          ? [{
              id: BULK_DELETE,
              label: TASK_ACTIONS.delete.bulk(pickedDrafts.length),
              icon: ACTION_ICON.delete,
              destructive: true,
              disabled: !!bulkRuns[BULK_DELETE] || pickedDrafts.length === 0,
              disabledReason: runReason(BULK_DELETE) ?? 'No drafts picked',
              onRun: () => void runBulkVerb(BULK_DELETE, pickedDrafts, {
                title: `Delete ${countOf(pickedDrafts.length, 'draft')}?`,
                /* An epic draft takes its stage tasks with it (`delete_task_core`'s cascade): said when one is picked. */
                message: pickedDrafts.some(task => task.persona === 'epic')
                  ? 'Each draft, an epic\'s stage tasks and their attachments are deleted for good — this cannot be undone.'
                  : 'Each draft and its attachments are deleted for good — this cannot be undone.',
                confirmLabel: TASK_ACTIONS.delete.verb,
              }, { doing: 'Deleting', done: 'Deleted' }),
            }]
          : []),
        ...(bulkVerbOffered(BULK_ARCHIVE, pickedArchivable.length)
          ? [{
              id: BULK_ARCHIVE,
              label: TASK_ACTIONS.archive.bulk(pickedArchivable.length),
              icon: ACTION_ICON.archive,
              destructive: true,
              disabled: !!bulkRuns[BULK_ARCHIVE] || pickedArchivable.length === 0,
              disabledReason: runReason(BULK_ARCHIVE) ?? 'No finished tasks picked',
              onRun: () => void runBulkVerb(BULK_ARCHIVE, pickedArchivable, {
                title: `Archive ${countOf(pickedArchivable.length, 'task')}?`,
                /* The row's Archive and this one are the same `DELETE /api/tasks/{id}` (`delete_task_core`): the row,
                   its subtasks, attachments and manifests go for good — said as the row's own confirm says it. */
                message: 'Each task, its subtasks and attachments are deleted for good — this cannot be undone.',
                confirmLabel: TASK_ACTIONS.archive.verb,
              }, { doing: 'Archiving', done: 'Archived' }),
            }]
          : []),
      ]
  }

  return (
    <Box data-testid="tasks-page" {...{ [SHELL_FILL_ATTR]: '' }} sx={TASKS_PAGE_SX}>
      <PaneHeader
        scope={scope}
        tree={taskTree}
        onNavigate={navigateScope}
        creators={orderedCreators}
        currentUserId={admin?.id ?? null}
        selectAll={chipSelectAll}
        toolbar={{
          focus,
          onFocusChange: changeFocus,
          onClearFilters: clearFilters,
          showHidden,
          onShowHiddenChange: changeShowHidden,
          q,
          onQChange: changeQ,
          onQClear: clearQ,
          /* THE CREATOR FILTER'S SECOND SURFACE — the same filter as the avatar strip (same roster, ids, "(you)",
             setter); the menu's Creator page is the narrow-viewport path and what the badge counts. */
          creators: orderedCreators,
          currentUserId: admin?.id ?? null,
          creatorIds,
          onCreatorIdsChange: changeCreatorIds,
          taskSources,
          sources,
          onSourcesChange: changeSources,
          stateFilter,
          onStateFilterChange: changeStateFilter,
          groupBy: effectiveGroupBy,
          onGroupByChange: changeGroupBy,
          groupByOptions,
          dateRange,
          dateFrom,
          dateTo,
          onDateRangeChange: changeDateRange,
          onDateFromChange: changeDateFrom,
          onDateToChange: changeDateTo,
          /* AC1c.1: navigates to the standalone New page. ONLY FOR A `tasks:create` HOLDER — `/tasks/new` is
             guarded by `RequireFeature permission="tasks:create"` (`App.tsx`); no handler, no button. It carries the
             board's scope like the create row's Expand (single home: the `leadingRow` prop). */
          onNewTask: canCreateTasks
            ? () => {
                const target = createTargetRef.current
                /* Another org's project: the editor opens acting as it, as the row's ⤢ does. */
                const actAsOrg = actAsNeededFor(
                  allProjects.find(p => p.id === target?.projectId), admin?.is_operator === true)
                if (actAsOrg) enterActAs(actAsOrg)
                navigate('/tasks/new', { state: expandState(takeInlineDraft(), target) })
              }
            : undefined,
          // The template gallery, for the same creator: its Start and Customize file the task.
          onFromTemplate: canCreateTasks ? () => navigate('/templates') : undefined,
          /* ITS TWIN onto the chat create surface, carrying the SAME board scope; gated on `canCreateChats`. */
          onNewChat: canCreateChats
            ? () => {
                // Another org's project: the chat opens acting as it, as New task does.
                const actAsOrg = actAsNeededFor(
                  allProjects.find(p => p.id === boardProjectId), admin?.is_operator === true)
                if (actAsOrg) enterActAs(actAsOrg)
                navigate('/tasks/new-chat', { state: { boardProjectId } satisfies NewTaskNavState })
              }
            : undefined,
        }}
      />

      {error && <Banner severity="error">{error}</Banner>}
      <QueuedOverCap />
      {actionError && (
        <Banner severity="error" testId="task-action-error" onDismiss={() => setActionError(null)}>
          {actionError.text}
        </Banner>
      )}

      {confirmDialog}

      {/* Both refetch on success: the row may leave this scope (a filing) or change section (a wait). The rail's
          counts are the dialogs' own invalidation. */}
      <MoveToFeatureDialog
        task={filingTask}
        tree={taskTree}
        onClose={() => setFilingTask(null)}
        onMoved={() => void fetchTasks()}
      />
      <WaitOnCustomerDialog
        task={waitingTask}
        onClose={() => setWaitingTask(null)}
        onDone={() => void fetchTasks()}
      />

      {/* THE PREEMPT DIALOG, PAGE-mounted like `confirmDialog` (`useTaskActions`). */}
      {preemptDialog}
      <PriorityDialog
        task={priorityTask}
        onClose={() => setPriorityTask(null)}
        onDone={updated => void priorityChanged(updated)}
      />
      {priorityPreempt && (
        <PreemptDialog
          open
          target={priorityPreempt.task}
          forecast={priorityPreempt.forecast}
          onForceStart={stopId => void priorityForceStart(priorityPreempt.task, stopId)}
          onQueue={() => setPriorityPreempt(null)}
          onCancel={() => setPriorityPreempt(null)}
        />
      )}

      {/* BA R9/AC9.2: a deep link that cannot reach its target SAYS SO. Dismiss stays a NAMED button (the suite
          drives "Dismiss"), not `Banner`'s ✕. */}
      {deepLinkError && (
        <Banner severity="error">
          <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            {deepLinkError}
            <Button variant="text" onClick={() => setDeepLinkError('')}>
              Dismiss
            </Button>
          </Box>
        </Banner>
      )}

      {/* THE LIST'S OWN SCROLL REGION above the phone (`LIST_SCROLL_SX`); on the phone a plain box in `.app-main`'s
          scroll. Everything that scrolls with the rows sits in it; the header above and the dock below do not. */}
      <Box
        ref={listScrollRef}
        data-testid="tasks-list-scroll"
        sx={LIST_SCROLL_SX}
      >
      {/* THE FEATURE HEADER (AC-5.4): the selection is one feature, whichever style draws its rows. */}
      {scope.kind === 'feature' && (
        <FeatureHeader
          featureId={scope.featureId}
          counts={scopedFeatureLocation.feature?.counts}
          treeName={scopedFeatureLocation.feature?.name}
          // Its tasks are now the project's own: land there.
          onDeleted={projectId => navigate(scopeHref({ kind: 'project', projectId }), { replace: true })}
        />
      )}

      {/* Explains the pinned-in epic row above a filtered-looking list. */}
      {pinnedIsMerged && pinnedTask && (
        <Typography variant="caption" component="div" role="status" data-testid="list-truncation-note" sx={NOTE_SX}>
          Task #{pinnedTask.id} is outside the loaded list — pinned to the top.
        </Typography>
      )}

      {/* R4: never truncate silently — only when the window hit the server's ceiling with rows behind it; points at
          the filters. */}
      {!loading && limit >= TASK_LIST_MAX && nextCursor !== null && (
        <Typography variant="caption" component="div" role="status" data-testid="list-truncation-note" sx={NOTE_SX}>
          Showing the first {TASK_LIST_MAX} tasks — some may be hidden. Pick a project to narrow the
          list.
        </Typography>
      )}

      <ActivityFeedView
        tasks={displayTasks}
        loading={loading}
        deepLinkedId={deepLinkedId}
        groupBy={effectiveGroupBy}
        /* THE HOST'S MEMOIZED OBJECT, never a fresh literal: the feed memoizes sectioning on it and the recency cut
           reads `Date.now()`. The section toggle rides as its own prop. */
        grouping={grouping}
        onToggleSection={toggleSection}
        stateGrouping={stateGrouping}
        tree={taskTree}
        actions={rowActions}
        submitting={submitting}
        expanded={expanded}
        /* THE FOCUS TOGGLE narrows an expanded epic's children — the half the server cannot do
           (`listViews/types.ts`). */
        focus={focus}
        filtered={hasTaskUrlFilters({ ...urlFilters, showHidden: false }) || focus}
        fetching={fetching}
        onToggleTask={id => void toggleExpand(id)}
        create={listCreate}
        selection={boardSelection}
      />

      {/* Under the unfiltered board, a young org (at most `STARTER_MAX_TASKS` tasks, which `TemplateStarters` checks
          against the org-wide count itself; more rows on screen already rule it out) gets the setup diagram, then the
          Most popular templates; any filter keeps the ordinary list. */}
      {!loading && displayTasks.length <= STARTER_MAX_TASKS && !hasTaskUrlFilters({ ...urlFilters, showHidden: false }) && !focus && (
        <Suspense fallback={null}>
          <TemplateStarters />
        </Suspense>
      )}

      {/* THE LAZY-LOAD SENTINEL: a zero-height div after the list; in view (with 400px of runway) the window grows a
          page. Rendered only while more is plausible. A div, not the last row: the list must not carry this page's
          fetch policy. `aria-hidden` + `role="presentation"`. */}
      {canLoadMore && (
        <Box
          ref={sentinelRef}
          data-testid="tasks-load-more-sentinel"
          aria-hidden="true"
          role="presentation"
          sx={{ height: 1 }}
        />
      )}
      </Box>

      {/* THE BULK BAR, STUCK TO THE BOTTOM of the scrollport: drafts sit low on a State board, and a bar above the
          list would be out of sight while the operator picks them. Marked as a bottom bar so the Help launcher
          sits level with it, past its right end (`FORM_ACTION_BAR_ATTR`). */}
      {(bulkDockShown || pickNote !== null) && (
        <Box ref={bulkDockRef} data-testid="tasks-bulk-dock" {...{ [FORM_ACTION_BAR_ATTR]: '' }} sx={BULK_DOCK_SX}>
          {/* What the last draw dropped from the pick, where the operator was looking for the pick. */}
          {pickNote !== null && (
            <Typography variant="caption" component="div" role="status" data-testid="tasks-pick-note" sx={{ color: 'text.secondary' }}>
              {pickNote}
            </Typography>
          )}
          {bulkDockShown && (
            <TaskBulkDock store={pickStore} actionsFor={bulkActionsFor} onClear={clearPicked} runs={bulkRuns} reports={bulkReports} />
          )}
        </Box>
      )}

    </Box>
  )
}
