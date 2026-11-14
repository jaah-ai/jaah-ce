import { Suspense, startTransition, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps } from 'react'
import { lazyRetry } from '../lib/lazyRetry'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { keepPreviousData, replaceEqualDeep, useIsRestoring, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { ApiError, apiDetail, get, getPage, put } from '../api'
import { useAuth, useCan, useCanOnProject } from '../auth'
import { useViewerMode } from '../lib/viewerMode'
import Banner from '../ui/Banner'
import { useConfirm, type ConfirmOptions } from '../components/ConfirmDialog'
import { useBusyIds } from '../hooks/useBusyIds'
import { useTaskPins } from '../hooks/useTaskPins'
import { slotConsoleRequest } from '../hooks/useWorktreeAccess'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import { useProjects, useWorktrees, WORKTREES_KEY } from '../hooks/useRosters'
import { useTagGroups } from '../hooks/useTagGroups'
import { CATALOGUE_STALE_MS } from '../lib/cachedRead'
import { useConsoleDock } from '../components/ConsoleDock'
import type { StartIntent } from '../components/tasks/controls'
import { useDeleteTask, useTaskActions } from '../components/tasks/useTaskActions'
import { ActingForNotice, useOrgVisible, useOtherOrgOf } from '../ui/OrgIndicator'
import { readHiddenColumns, TASK_COLUMNS, toggledHiddenColumns } from '../lib/taskColumns'
import { loadTaskDetailPage } from './loadTaskDetailPage'
import PaneHeader from '../components/tasks/pane/PaneHeader'
import {
  ACTION_ICON,
  buildTagCountsUrl,
  buildTaskAssigneesUrl,
  buildTaskCreatorsUrl,
  buildTasksUrl,
  displayTitle,
  type ExpandedEntry,
  drawnExpandedIds,
  drawnPendingIds,
  isArchiveRow,
  LIVE_STATES,
  TASK_ACTIONS,
  TASK_LIST_MAX,
  TASK_PAGE_SIZE,
  TASK_SOURCES_URL,
  actAsNeededFor,
} from '../components/tasks/shared'
import { enterActAs, useActAsRows } from '../lib/actAs'
import type { TaskTreeGrouping } from '../components/tasks/taskTree'
import ActivityFeedView from '../components/tasks/listViews/ActivityFeedView'
import type { PaneSelection, TaskListCreate, TaskListStateGrouping } from '../components/tasks/listViews/types'
import FeatureDescription from '../components/tasks/pane/FeatureDescription'
import { PANE_QUERY_KEY, forgetDeletedTasks } from '../components/tasks/pane/queryKeys'
import { paneCountsUrl, type PaneFilters } from '../components/tasks/pane/boardCounts'
import { boardRowsEpoch, rememberBoardRows } from '../lib/taskPageSeed'
import { readTaskListSeed, saveTaskListSeed } from '../lib/taskListSeed'
import { TASK_SEEN } from '../lib/taskSeen'
import { TASK_PINS_SAVED } from '../lib/taskPin'
import { muteExclusionParams, readMuted } from '../lib/taskMute'
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
import { useLoadMoreSentinel } from '../hooks/useLoadMoreSentinel'
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
  DEFAULT_GROUP_BY,
  GROUP_BY_PROJECT,
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
import { isSavedReturn, SAVED_RETURN_STATE, savedReturnStage, type SavedReturn } from '../lib/taskReturn'
import {
  expandedTasksStorageKey,
  loadExpandedTaskIds,
  saveExpandedTaskIds,
} from '../lib/taskExpandStorage'
import { instantWindowBounds, resolveDateRangeBounds, type TaskDateRangeId } from '../lib/taskDateRange'
import { useOfficialDayStart, useOfficialZone } from '../lib/officialZone'
import { orderAssignees } from '../lib/taskAssignees'
import {
  loadTaskFilters, rememberedFingerprint, rememberedViewKey, restoreKeepingStage, restoreRememberedView, saveRememberedView,
  saveTaskFilters, taskFiltersStorageKey,
} from '../lib/taskFilterStorage'
import {
  clearTaskUrlFilters,
  hasTaskUrlFilters,
  readTaskUrlFilters,
  writeTaskUrlFilters,
  type TaskUrlFilters,
} from '../lib/taskUrlFilters'
import { runnerWorktreeFor, worktreeForTask } from '../lib/worktree'
import { publishBoardScope } from '../lib/boardScope'
import {
  ALL_SCOPE,
  SCOPE_PARAM,
  locateScope,
  parseScope,
  scopeKey,
  scopeProjectId,
  scopeToListParams,
  taskInScope,
  type TaskScope,
} from '../lib/taskScope'
import { TASK_TREE_QUERY_KEY, useTaskTree } from '../hooks/useTaskTree'
import { FOCUS_COMPOSER_STATE } from '../components/tasks/rail/TaskTree'
import { ABOVE_PHONE, SHELL_FILL_ATTR, TASK_BOARD_CONTAINER_SX } from '../ui/listStyles'
import QueuedOverCap from './ce/QueuedOverCap'
import StageStrip, { StageTotals, type StripPick } from '../components/pipeline/StageStrip'
import PipelineSections from '../components/pipeline/PipelineSections'
import ReleaseStep from '../components/pipeline/ReleaseStep'
import { HEALTH_POLL_MS } from '../components/pipeline/HealthDot'
import ReleasePick from '../components/pipeline/ReleasePick'
import { FrozenNowBanner, stepFrom } from '../components/pipeline/WaitChip'
import { hasDeadline } from '../lib/waitReasons'
import { releaseKey } from '../lib/releases'
import {
  ONLY_PARAM,
  PIPELINE_ALL_ITEMS_KEY,
  PIPELINE_TOTALS_KEY,
  RELEASE_ONLY,
  releaseRunTo,
  STAGE_PARAM,
  BUCKETS,
  isBucket,
  refreshPipelineItems,
  getAllStageItems,
  getPipelineTotals,
  isUnfiltered,
  getPipelineView,
  pipelineAllItemsKey,
  pipelineTotalsKey,
  pipelineViewKey,
  stageTasksAcross,
  totalsStage,
  attentionStage,
  AUTO_STAGE_STATE,
  type ItemFilters,
} from '../lib/pipelineBoard'
import { STARTER_MAX_TASKS } from './templates/sections'
import { LAUNCHER_CLEARANCE_PX, useLauncherYield } from '../ui/launcherSeat'
import { grid } from '../theme'
import { useOutageCovers, useRecoveries } from '../net/reachability'
import type {
  OrgRef,
  PipelineTask,
  Project,
  TagCounts,
  TagGroup,
  TaskAssignee,
  TaskSourceOption,
  TaskTreeCounts,
  Worktree,
} from '../types'

/* The board's starters — the setup diagram, then Most popular templates (`TemplateStarters`): their own chunk. */
const TemplateStarters = lazyRetry(() => import('./templates/TemplateStarters'))

// AC1c.3: the create/edit form lives on the standalone page (`/tasks/new`, `/tasks/:id`); this route neither
// imports its ~500 KB TipTap chunk nor holds any modal state. It only PREFETCHES that page's chunk once mounted
// (`loadTaskDetailPage`), so the first row press opens a page already downloaded.

const METERED_REFETCH_MIN_MS = 60_000

/* The board's own floor between live refetches. Each one re-reads the list, every drawn expanded row's detail
   and both counts (hundreds of KB on a big board), and the shared debounce bounds a burst only while
   it stays dense: events a second apart, as one busy chat parking and resuming every turn, re-read the whole
   board for every open tab once a second. The first event still refetches at once. */
const TASKS_LIVE_REFETCH_MIN_MS = 5_000

/* THE BOARD'S EXPANDED-ROW READ: the row's `children` and their costs, none of the detail page's own fields
   (`routes/tasks/read.py::get_task`, `view=rollup`) — the board draws the parent from its list row. */
const expandedDetailUrl = (id: number) => `/api/tasks/${id}?view=rollup`

/* The console events that move the worktree set the row actions join on (`WORKTREES_KEY`). */
const WORKTREE_EVENT_ENTITIES = new Set(['worktree', 'runner'])

/* The worktree set's own floor on `task` events (a lease edge), apart from the board's: a busy board re-read the
   whole pool on every transition. */
const WORKTREES_TASK_REFETCH_MIN_MS = 30_000

const NO_PROJECTS: Project[] = []
const NO_WORKTREES: Worktree[] = []
const NO_TAG_GROUPS: TagGroup[] = []
const NO_ASSIGNEES: TaskAssignee[] = []
const NO_SOURCES: TaskSourceOption[] = []
const NO_TASKS: PipelineTask[] = []
/** The longest a return from Save holds the list while it looks the landing stage up. */
const STAGE_LOOKUP_MAX_MS = 5000

/* A quiet hint above the list; colour from the `caption` variant. */
const NOTE_SX = { color: 'text.secondary', mb: 1 } as const

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

/* THE /tasks PAGE is the BOARD's query container: the header's one line and the activity row's drop order follow its
   width, which the docked console narrows without the window changing (`ui/listStyles.ts`'s `BOARD_*`).
   ABOVE THE PHONE IT FILLS THE SHELL: a column of the header, the list's OWN scroll region and the bulk dock
   under it (`SHELL_FILL_ATTR`, so `.app-main` keeps its plain bottom inset). The dock used to stick to `.app-main`'s
   foot with rows sliding beneath it; now no row is ever under it, and it spans the list's width. The phone keeps the
   shell's scroll (its bottom nav and onboarding bar are sized against `.app-main`). */
const TASKS_PAGE_SX = {
  ...TASK_BOARD_CONTAINER_SX,
  [ABOVE_PHONE]: { height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' },
} as const

/* The list's scroll region above the phone (see `TASKS_PAGE_SX`). Its bottom inset is the Help launcher's clearance
   (`LAUNCHER_CLEARANCE_PX`, 88px) less the shell's own bottom inset (~1.75rem), so the last row's `⋯` scrolls clear of the orb; with the dock up the orb is hidden
   (`useLauncherYield`). Its scrollport clips sideways too, so it reaches 3px back into the page's inset and pads it
   in again: a selected disc's ring, which overhangs the row by 1px, stays whole. */
const LIST_SCROLL_SX = {
  [ABOVE_PHONE]: {
    flex: 1, minHeight: 0, overflowY: 'auto', ml: -0.5, pl: 0.5,
    pb: (t: Theme) => `calc(${t.spacing(LAUNCHER_CLEARANCE_PX / 6)} - ${grid(5)})`,
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

/* THE PIPELINE'S OWN LIVE FLOOR (architecture §4): a `pipeline.changed` burst re-reads the strip and the items at
   most once per 2 s, and every 10 s while the stream is down or a step is in flight. */
const PIPELINE_LIVE_MIN_MS = 2_000
const PIPELINE_POLL_MS = 10_000

/** The board. `pipeline` (the `/tasks` route, `pages/pipeline/PipelinePage.tsx`) puts the stage strip above search
 *  and, on one project, lists its stages' items in sections (`components/pipeline/`); the Live Cast viewer omits it. */
export default function Tasks({ pipeline = false }: { pipeline?: boolean } = {}) {
  // "New task" and a row's Edit are NAVIGATIONS to the standalone page.
  const navigate = useNavigate()
  const viewer = useViewerMode()
  const [tasks, setTasks] = useState<PipelineTask[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // What the list read threw: an unanswered read is the outage notice's to say, not this page's banner.
  const [errorCause, setErrorCause] = useState<unknown>(null)
  const outageCoversError = useOutageCovers(errorCause)
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
  // rung also asks the grant map (`resolveCreateProject` in `components/tasks/shared.tsx`). Read after the list,
  // never ahead of it (`listLanded`); failure costs name lookups and the create row shows its no-project hint.
  /* THE LIST GOES FIRST: the reads the rows do not wait on (assignees, sources, tag groups, worktrees, the project
     roster) start once the first list read has settled, so on a cold load they do not queue ahead of it on the API.
     Each still paints from the persisted cache meanwhile where it has one. Flips once, never back. */
  const [listLanded, setListLanded] = useState(false)
  const projectsQuery = useProjects({ enabled: listLanded })
  const allProjects = Array.isArray(projectsQuery.data) ? projectsQuery.data : NO_PROJECTS
  // Did that fetch ANSWER: `[]` alone is pending, failed or a new org with no project (the create row tells them apart).
  const projectsAnswered = Array.isArray(projectsQuery.data)
  /* ── TAG GROUPING ──────────────────────────────────────────────────────────
     THE CATALOG IS FETCHED, not derived from loaded rows (which would reshuffle sections while scrolling):
     `hooks/useTagGroups.ts`, after the list. The reconciliation below DELETES a stored selection, so it runs only
     against a catalog THIS admin was answered (`isSuccess`, keyed on the admin there): a failed fetch is not an
     answer, and meanwhile the board renders ungrouped. */
  // Re-read on every board visit: Settings › Tags edits the catalog without invalidating the key.
  const tagGroupsQuery = useTagGroups({ enabled: listLanded, refetchOnMount: 'always' })
  const tagGroups = tagGroupsQuery.data ?? NO_TAG_GROUPS

  /* FOLDED SECTIONS, keyed by TAG ID (`''` = Untagged), held HERE: sentinel appends can change the section set
     (index keys would move folds), and a remounted child would re-open. In memory only. */
  const [sectionCollapsed, setSectionCollapsed] = useState<Record<string, boolean>>({})
  // FOCUS is restored from `sessionStorage` (`lib/taskFilterStorage.ts` says why the SESSION; everything else
  // the board shows lives in the URL, below). Restored IN THE STATE INITIALIZER, not an effect: the mount fetch would otherwise request and paint the unfiltered board first.
  // `admin` is available this early (`auth.tsx` seeds from the persisted snapshot); the restore effect below
  // covers a late or changed id.
  const { admin, updatePreferences } = useAuth()
  const { isPinned, onTogglePin } = useTaskPins()
  const filtersStorageKey = taskFiltersStorageKey(admin?.id ?? null)
  // ONE read at mount (lazy initializer).
  const [restoredFilters] = useState(() => loadTaskFilters(filtersStorageKey))

  /* ── AND THE ROW-REMOVING FILTERS, WHICH ARE THE URL'S ───────────────────────────────────────────
     Search, period, assignee picks and Hidden are read from and written to the query string
     (`lib/taskUrlFilters.ts` is the single home): a narrowed board is a link one operator sends another. One
     owner per value. `useSearchParams` answers on the FIRST render, so the first request still carries them;
     a URL naming no filter opens with its selection's remembered ones (THE REMEMBERED FILTERS, below).
     DECLARED ONCE, above every reader: the deep-link consumer below shares it (two calls = two writers). */
  const [searchParams, setSearchParams] = useSearchParams()
  /* Memoised on the router-memoised params, so the object and assignee ARRAY keep identity for effect keys. */
  const urlFilters = useMemo(() => readTaskUrlFilters(searchParams), [searchParams])
  const {
    q, showHidden, dateRange, dateFrom, dateTo, assigneeIds, creatorIds, sources, state: stateFilter, persona, windowAfter,
    windowBefore,
  } = urlFilters
  /* A SCALAR for the fetch effects' deps: non-filter URL changes (the deep-link strip) hand back a fresh array
     with identical contents. Same idiom as `expandedIdsKey`. */
  const assigneeKey = assigneeIds.join(' ')
  const creatorKey = creatorIds.join(' ')
  const sourceKey = sources.join(' ')

  /* THE NEWEST PARAMS THIS PAGE KNOWS OF — the router's, or those a write of ours just issued. EVERY write builds
     on this, never on its closure.
     `setSearchParams` DOES NOT COMPOSE: react-router's functional form resolves against THIS RENDER's params
     and navigates immediately (react-router-dom 6.30), so two patches in one gesture lose the first silently.
     That is the Clear gesture (four filters in one press, `clearFilters` below), which otherwise left
     the URL and board narrowed. Effect closures (the assignee reconcile) are stale the same way, hence a ref.
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
  const { tree: taskTree, failed: taskTreeFailed } = useTaskTree(true)
  const rawScope = searchParams.get(SCOPE_PARAM)
  /* A bare `?project=N` (no `?task=`, no `?scope=`) names the project to open on: read as the selection at once
     (so the first fetch is that project's, not All projects') and written into the URL as its `?scope=`,
     REPLACED so Back does not bounce through All projects. A `?task=` deep link moves the selection itself
     (below) and wins. */
  const linkedProjectId = rawScope === null && !searchParams.has(TASK_DEEP_LINK_PARAM)
    ? parseTaskDeepLinkProjectParam(searchParams.get(TASK_DEEP_LINK_PROJECT_PARAM))
    : null
  const landingScope = linkedProjectId !== null
    ? scopeKey({ kind: 'project', projectId: linkedProjectId })
    : null
  const effectiveRawScope = landingScope ?? rawScope
  const scope: TaskScope = useMemo(() => parseScope(effectiveRawScope), [effectiveRawScope])
  useEffect(() => {
    if (landingScope === null) return
    const next = new URLSearchParams(liveParamsRef.current)
    next.set(SCOPE_PARAM, landingScope)
    next.delete(TASK_DEEP_LINK_PROJECT_PARAM)
    writeUrl(next, { replace: true })
  }, [landingScope])
  /* THE REMEMBERED FILTERS (`lib/taskFilterStorage.ts::restoreRememberedView`), per selection in this browser: a
     selection landed on with no filter in its URL gets back the ones it last had — search and the stage strip's pick
     included — and switching selection swaps the carried filters for the new one's (dropping the old stage pick).
     REPLACED, so Back does not walk it. A deep link waits until it is consumed (`?task=` stripped), so a return from
     a task lands on its filters. A LAYOUT effect reading this render's params, so the remembered board paints first.
     Only a change of the filters after that is remembered: `pendingViewRef` holds the save until our own write lands. */
  const viewKey = rememberedViewKey(admin?.id ?? null, landingScope ?? rawScope)
  const restoredViewRef = useRef<string | null>(null)
  const viewSeenRef = useRef<string | null>(null)
  const pendingViewRef = useRef<string | null>(null)
  /* The stage a return from the task editor's Save landed on (`lib/taskReturn.ts`): the restore keeps it over the
     remembered pick. Held from the landing location, which the deep-link strip replaces before the restore runs. */
  const landingLocation = useLocation()
  const handedStageRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    const ret = (landingLocation.state as Record<string, unknown> | null)?.[SAVED_RETURN_STATE]
    if (isSavedReturn(ret) && ret.stage !== null) handedStageRef.current = ret.stage
  }, [landingLocation.key]) // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    if (restoredViewRef.current === viewKey || searchParams.has(TASK_DEEP_LINK_PARAM)) return
    const switching = restoredViewRef.current !== null
    restoredViewRef.current = viewKey
    const handed = handedStageRef.current
    handedStageRef.current = null
    const next = handed !== null && searchParams.get(STAGE_PARAM) === handed
      ? restoreKeepingStage(viewKey, searchParams, handed, switching)
      : restoreRememberedView(viewKey, searchParams, { switching })
    viewSeenRef.current = rememberedFingerprint(next ?? searchParams)
    pendingViewRef.current = next === null ? null : viewSeenRef.current
    if (next !== null) writeUrl(next, { replace: true, resetWindow: false })
  }, [viewKey, searchParams])
  useEffect(() => {
    if (restoredViewRef.current !== viewKey || searchParams.has(TASK_DEEP_LINK_PARAM)) return
    const seen = rememberedFingerprint(searchParams)
    if (pendingViewRef.current !== null) {
      if (seen === pendingViewRef.current) pendingViewRef.current = null
      return
    }
    if (seen === viewSeenRef.current) return
    viewSeenRef.current = seen
    saveRememberedView(viewKey, searchParams)
  }, [searchParams, viewKey])
  /* THE ONE PROJECT the selection lives in — what new tasks are filed into and what the create ladder calls the
     board's project — or null for All projects or a feature the tree has not named yet. */
  const boardProjectId = scopeProjectId(scope, taskTree)
  /* ── THE PIPELINE (`pipeline` only) ── one project's stages: the strip's read, the pick in the URL. */
  const pipelineProjectId = pipeline && !viewer ? boardProjectId : null
  const stagePick: StripPick = { stage: searchParams.get(STAGE_PARAM), only: searchParams.get(ONLY_PARAM) }
  /* A feature scope's feature, for the inline create rows to file into (`resolveCreateTarget`). */
  const scopedFeatureLocation = scope.kind === 'feature' ? locateScope(scope, taskTree) : {}
  /* Memoised on its four facts, so the create bundle (`listCreate`) keeps its identity across a refetch. */
  const scopedFeatureId = scopedFeatureLocation.feature?.id
  const scopedFeatureName = scopedFeatureLocation.feature?.name
  const scopedFeatureProjectId = scopedFeatureLocation.project?.id
  const scopedFeature = useMemo(
    () => scopedFeatureId !== undefined && scopedFeatureName !== undefined && scopedFeatureProjectId !== undefined
      ? { id: scopedFeatureId, name: scopedFeatureName, projectId: scopedFeatureProjectId }
      : undefined,
    [scopedFeatureId, scopedFeatureName, scopedFeatureProjectId],
  )
  /* WHAT ⤢ HANDS `/tasks/new` BESIDE THE PROJECT: the scope's feature, so the editor files where the row said it
     would (`TaskDetailPage` re-validates it against the tree). */
  const boardFeatureId = scopedFeature?.id ?? null
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
  /* The task page's chunk, fetched once the board is up. A failed prefetch is silent: the row press's own
     `lazyRetry()` load asks again and reports there. */
  useEffect(() => {
    loadTaskDetailPage().catch(() => {})
  }, [])
  /* THE USER'S MUTES (`lib/taskMute.ts`): the muted nodes above the scope leave its row set. A STRING for memo
     and effect deps, as `assigneeKey`. */
  const mutedKey = readMuted(admin?.preferences).join(' ')
  const muted = useMemo(() => (mutedKey === '' ? [] : mutedKey.split(' ')), [mutedKey])
  const listScope = scopeToListParams(scope, muted)
  const listScopeKey = listScope.toString()
  // "Show hidden" — the menu's ONE additive switch: hidden AND archived rows together, on one tick (one boolean
  // end to end). Off by default.
  // MOSTLY SYSTEM ROWS, NOT ONLY: a Runner cron's `hidden` tick (`app/cron_activity_runner.py`) files the
  // operator's own maintenance hidden. Both classes land in the board's FINISHED half (a hidden row asking a
  // question is on the board anyway, unfinished)
  // (`routes/tasks/listing.py::_TASK_LIST_COMPLETED_GROUP` orders the response), so ticking this never
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
    /* A link's exact window of instants (`?after=`/`?before=`) wins over the Period choice while present. */
    return instantWindowBounds(windowAfter, windowBefore) ?? resolveDateRangeBounds(dateRange, dateFrom, dateTo, zone)
  }

  /* WHOSE ROWS — assignee ids, EMPTY for everybody. A real filter, the URL's. Two surfaces drive it (the header's
     avatar strip and the menu's Assignee page, operator's call) and share one setter and one roster. */
  /* THE ROSTER (`GET /api/tasks/assignees`): people with top-level tasks in the CURRENT PROJECT SCOPE. Fetched,
     never derived from the lazy window (as `tagGroups`). Refetched on what changes WHO HAS ROWS (project scope,
     Show hidden) and NOT on the period or the pick — the picker must keep offering the people it filters away
     (`buildTaskAssigneesUrl` is the single home for the roster URL's params). Read once the list has landed.
     KEYED ON THE ADMIN TOO: the roster always carries the caller's own row, so an in-place admin switch must
     refetch. The last roster stays on screen while the next loads. The board never depends on it: a failed read
     costs this visit the picker, keeps the filter. */
  const assigneesUrl = buildTaskAssigneesUrl(listScope, showHidden)
  const assigneesQuery = useQuery({
    queryKey: ['task-assignees', assigneesUrl, admin?.id ?? null],
    queryFn: () => get<TaskAssignee[]>(assigneesUrl).then(roster => (Array.isArray(roster) ? roster : [])),
    enabled: listLanded,
    placeholderData: keepPreviousData,
    meta: { persist: false },
  })
  const assignees = assigneesQuery.data ?? NO_ASSIGNEES
  /* RECONCILES THE RESTORED PICK ONLY ON SUCCESS (`dataUpdatedAt` moves on each answer, never on a failure or a
     held placeholder): an id with no rows in the new scope is an invisible narrowing, but a FAILED fetch is not that
     answer (a transient 500 must not widen a chosen filter) — as the tag catalog. */
  useEffect(() => {
    if (assigneesQuery.isSuccess && !assigneesQuery.isPlaceholderData) {
      keepKnown('assigneeIds', new Set(assigneesQuery.data.map(assignee => assignee.id)))
    }
  }, [assigneesQuery.dataUpdatedAt])
  /* THE CREATOR ROSTER (`GET /api/tasks/creators`): the assignee roster's twin — same scope, same refetch keys,
     same success-only reconciliation — whose "System" entry covers every row no human filed. */
  const creatorsUrl = buildTaskCreatorsUrl(listScope, showHidden)
  const creatorsQuery = useQuery({
    queryKey: ['task-creators', creatorsUrl, admin?.id ?? null],
    queryFn: () => get<TaskAssignee[]>(creatorsUrl).then(roster => (Array.isArray(roster) ? roster : [])),
    enabled: listLanded,
    placeholderData: keepPreviousData,
    meta: { persist: false },
  })
  const creators = creatorsQuery.data ?? NO_ASSIGNEES
  useEffect(() => {
    if (creatorsQuery.isSuccess && !creatorsQuery.isPlaceholderData) {
      keepKnown('creatorIds', new Set(creatorsQuery.data.map(creator => creator.id)))
    }
  }, [creatorsQuery.dataUpdatedAt])
  /* THE SOURCE VOCABULARY (`GET /api/tasks/sources`): codes and the server's labels, which are never spelled
     here. Fixed per deploy, so one read shared with the triggers' labels (`readTaskSources`, the same key).
     Reconciled as the assignee pick is, and only on success: a code the server does not know is an invisible
     narrowing with no word for its chip. A failure costs only the picker and the chips' words. */
  const sourcesQuery = useQuery({
    queryKey: ['task-sources'],
    // The cache holds the RAW answer (`cachedRead`'s contract on this key); shaped on read.
    queryFn: () => get<TaskSourceOption[]>(TASK_SOURCES_URL),
    enabled: listLanded,
    staleTime: CATALOGUE_STALE_MS,
  })
  const taskSources = useMemo(
    () => (Array.isArray(sourcesQuery.data) ? sourcesQuery.data : NO_SOURCES),
    [sourcesQuery.data],
  )
  useEffect(() => {
    if (sourcesQuery.isSuccess) keepKnown('sources', new Set(taskSources.map(source => source.value)))
  }, [sourcesQuery.dataUpdatedAt])

  /* ── THE VIEW: GROUP BY ──────────
     The URL's (`?group=`), so a link reproduces the view; State by default — Project on the pipeline page's All
     projects. `effectiveGroupBy` is what this selection can honour (`resolveGroupBy`): Project only
     across projects, a tag group only while the catalog holds it. */
  const defaultGroupBy = pipeline && !viewer && scope.kind === 'all' ? GROUP_BY_PROJECT : DEFAULT_GROUP_BY
  const groupBy = parseGroupParam(searchParams.get(GROUP_PARAM), defaultGroupBy)
  const groupByOptions = useMemo(() => offeredGroupBys(scope.kind === 'all', tagGroups), [scope.kind, tagGroups])
  const effectiveGroupBy = resolveGroupBy(groupBy, groupByOptions, tagGroupsQuery.isSuccess, defaultGroupBy)
  /* Sectioning by State reads the server's counts (`stateGrouping` below). */
  const feedStateGrouped = effectiveGroupBy === GROUP_BY_STATE
  /* The Live Cast viewer has no preferences to keep the fold in, and a visitor came to see finished work: Done
     opens there by default, and the fold lives for the page. */
  const [viewerDoneExpanded, setViewerDoneExpanded] = useState(true)
  const doneExpanded = viewer ? viewerDoneExpanded : admin?.preferences?.task_done_expanded === true
  /* THE VIEWER'S COLUMNS (Filter & view > Columns, `lib/taskColumns.ts`): stored on the account. Organization is
     offered only to whoever sees it (`useOrgVisible`). The set is memoized: the feed's row cache keys on it. */
  const orgVisible = useOrgVisible()
  const offeredColumns = useMemo(() => TASK_COLUMNS.filter(column => column.key !== 'org' || orgVisible), [orgVisible])
  /* Keyed on a STRING, as `mutedKey`: every preferences save hands back a new array, which would rebuild every row. */
  const hiddenKey = viewer ? '' : readHiddenColumns(admin?.preferences).join(' ')
  const hiddenColumns = useMemo(() => (hiddenKey === '' ? [] : hiddenKey.split(' ')), [hiddenKey])
  const hiddenColumnSet = useMemo(() => new Set(hiddenColumns), [hiddenColumns])
  /* THE STATE BOARD'S WINDOW in the order it is drawn (`buildTasksUrl` says how): a folded Done costs no row. */
  /* AN EXPLICIT STATE: DONE FILTER OPENS DONE: folded, the window sent `state=done&exclude_done=true` — no
     row — while Select all named the counts' total, and a pick under a fold is dropped (`onDrawn`). */
  const doneOpen = doneExpanded || stateFilter === 'done'
  const windowShape = { sectionOrder: feedStateGrouped, excludeDone: feedStateGrouped && !doneOpen }
  const queryClient = useQueryClient()
  /* The pipeline's view and items read take the board's filters, the scope's feature and
     the persona: every count on the strip and the sections is the listed rows'. */
  const pipelineBounds = dateBounds()
  const pipelineFeature = scope.kind === 'feature' ? scope.featureId : null
  // The user's hidden nodes above the scope leave the strip's counts and the stage lists, as they leave the feed's.
  const pipelineExclusions = muteExclusionParams(scope, muted)
  const pipelineExclusionsKey = pipelineExclusions.join(' ')
  const pipelineFilters: ItemFilters = useMemo(() => ({
    q, assigneeIds, creatorIds, sources, state: stateFilter, focus, showHidden, persona,
    after: pipelineBounds.updatedAfter ?? null, before: pipelineBounds.updatedBefore ?? null,
    featureId: pipelineFeature, exclusions: pipelineExclusions,
  // Keyed on the values: `dateBounds()` and the id arrays are fresh each render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [q, assigneeKey, creatorKey, sourceKey, stateFilter, focus, showHidden, persona, pipelineBounds.updatedAfter,
    pipelineBounds.updatedBefore, pipelineFeature, pipelineExclusionsKey])
  const pipelineViewQuery = useQuery({
    queryKey: pipelineViewKey(pipelineProjectId ?? 0, pipelineFilters),
    queryFn: () => getPipelineView(pipelineProjectId!, pipelineFilters),
    enabled: pipelineProjectId !== null,
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === pipelineProjectId ? prev : undefined),
    // Re-read while something moves, or while a wait counts down to a moment (soak, timeout, hours, freeze); while a
    // stage shows a health dot, once a minute, so the dot stays live (the read keeps its stages checked every minute).
    refetchInterval: query => (query.state.data?.steps.some(step => step.in_flight > 0 || hasDeadline(step.reasons))
      ? PIPELINE_POLL_MS : query.state.data?.stages.some(stage => stage.health) ? HEALTH_POLL_MS : false),
    meta: { persist: false },
  })
  const pipelineView = pipelineProjectId !== null ? pipelineViewQuery.data : undefined
  /* All projects (secondary, D7): the totals under the list's filters; pressing one picks that stage (`?stage=` its
     name) and the list then holds that stage's tasks across projects — each project's first page. */
  const allProjectsPipeline = pipeline && !viewer && scope.kind === 'all'
  const pipelineTotalsQuery = useQuery({
    queryKey: pipelineTotalsKey(pipelineFilters),
    queryFn: () => getPipelineTotals(pipelineFilters),
    enabled: allProjectsPipeline,
    placeholderData: prev => prev,
    meta: { persist: false },
  })
  const pipelineTotals = pipelineTotalsQuery.data
  /* The picked stage, as the totals name it, once they hold it (a project's stage id carried over, a stale link, a
     total not read yet: no pick). */
  const allStageName = allProjectsPipeline && pipelineTotals && stagePick.stage !== null
    ? totalsStage(pipelineTotals.totals, stagePick.stage)
    : null
  const allStageQuery = useQuery({
    queryKey: pipelineAllItemsKey(allStageName ?? '', pipelineFilters),
    queryFn: ({ signal }) => getAllStageItems(allStageName!, pipelineFilters, signal),
    enabled: allStageName !== null,
    meta: { persist: false },
  })
  const allStage = useMemo(
    () => (allStageQuery.data ? stageTasksAcross(allStageQuery.data.projects) : undefined),
    [allStageQuery.data],
  )
  /* Only this project's events re-read its pipeline (the event's id is the project's); across projects, the totals
     and a picked stage's tasks. */
  useLiveRefetch(useEventStream(), () => {
    if (pipelineProjectId === null) {
      void queryClient.invalidateQueries({ queryKey: PIPELINE_TOTALS_KEY })
      void queryClient.invalidateQueries({ queryKey: PIPELINE_ALL_ITEMS_KEY })
      return
    }
    void queryClient.invalidateQueries({ queryKey: ['pipeline-view', pipelineProjectId] })
    refreshPipelineItems(queryClient, pipelineProjectId)
    void queryClient.invalidateQueries({ queryKey: releaseKey(pipelineProjectId) })
  }, pipeline ? PIPELINE_POLL_MS : null, {
    entity: 'pipeline',
    minIntervalMs: PIPELINE_LIVE_MIN_MS,
    match: event => pipelineProjectId === null || String(event.id) === String(pipelineProjectId),
  })
  /* A pick is a push (Back walks it); a new pick drops the group a step's count chose. Picking the current pick keeps
     it (a press selects, never toggles off). `replace`: the rail's opening pick, which Back skips. */
  function pickStage(next: StripPick, { replace = false } = {}) {
    const params = new URLSearchParams(liveParamsRef.current)
    if (next.stage) params.set(STAGE_PARAM, next.stage)
    else params.delete(STAGE_PARAM)
    if (next.only) params.set(ONLY_PARAM, next.only)
    else params.delete(ONLY_PARAM)
    if (params.toString() === liveParamsRef.current.toString()) return
    writeUrl(params, { replace, resetWindow: false })
  }
  /* A PROJECT OPENED FROM THE RAIL (`AUTO_STAGE_STATE`, its scope key) opens on the stage that needs someone
     (`attentionStage`): questions, then failures, then stopped tasks, the rightmost first; else Ideas. Held until that
     project's strip is read AFTER the click (never a cached one) — the remembered-view restore replaces the URL (and
     its state) before then. It takes over the restore's pending save. */
  const railLocation = useLocation()
  const autoStageRef = useRef<{ key: string; scope: string; at: number } | null>(null)
  const railScope = (railLocation.state as Record<string, unknown> | null)?.[AUTO_STAGE_STATE]
  useEffect(() => {
    if (typeof railScope === 'string') autoStageRef.current = { key: railLocation.key, scope: railScope, at: Date.now() }
  }, [railLocation.key]) // eslint-disable-line react-hooks/exhaustive-deps
  const autoStageView = pipelineViewQuery.isPlaceholderData ? undefined : pipelineView
  const autoStageReadAt = pipelineViewQuery.dataUpdatedAt
  useEffect(() => {
    const pending = autoStageRef.current
    if (!pending || pending.scope !== rawScope || !autoStageView) return
    if (autoStageReadAt < pending.at) {
      void pipelineViewQuery.refetch()
      return
    }
    autoStageRef.current = null
    pendingViewRef.current = null
    pickStage({ stage: attentionStage(autoStageView), only: null }, { replace: true })
  }, [autoStageView, autoStageReadAt, rawScope, railLocation.key]) // eslint-disable-line react-hooks/exhaustive-deps
  /* A RETURN FROM THE TASK EDITOR'S SAVE (`SAVED_RETURN_STATE`) opens on the saved row's stage (`lib/taskReturn.ts`):
     held like the rail's (the deep-link strip and the restore drop the state), resolved once the board's one project
     is known, and dropped if the operator moved to another scope or stage, or left, before it answered. */
  const savedReturnRef = useRef<SavedReturn | null>(null)
  const savedReturn = (railLocation.state as Record<string, unknown> | null)?.[SAVED_RETURN_STATE]
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  /* Looked up only when the link carries no stage; the list shows loading meanwhile, never every stage. */
  const looksUpStage = pipeline && isSavedReturn(savedReturn) && savedReturn.stage === null
  const [stageLookup, setStageLookup] = useState(looksUpStage)
  useEffect(() => {
    if (!looksUpStage) return
    savedReturnRef.current = savedReturn
    setStageLookup(true)
  }, [railLocation.key]) // eslint-disable-line react-hooks/exhaustive-deps
  // A backstop: a lookup that never answers must not hold the list for good.
  useEffect(() => {
    if (!stageLookup) return
    const giveUp = window.setTimeout(() => setStageLookup(false), STAGE_LOOKUP_MAX_MS)
    return () => window.clearTimeout(giveUp)
  }, [stageLookup])
  useEffect(() => {
    const ret = savedReturnRef.current
    // Wait for the deep link to settle `?scope=` (a tab with no earlier board lands on the row's project).
    if (!ret || searchParams.has(TASK_DEEP_LINK_PARAM)) return
    if (scope.kind !== 'all' && boardProjectId === null) {
      // A feature the loaded tree does not place names no project: nothing to look up, so the list shows.
      if (taskTree !== undefined || taskTreeFailed) {
        savedReturnRef.current = null
        setStageLookup(false)
      }
      return
    }
    savedReturnRef.current = null
    const scopeAtStart = effectiveRawScope
    const stageAtStart = liveParamsRef.current.get(STAGE_PARAM)
    void savedReturnStage(ret, scope, boardProjectId).then(stage => {
      if (!mountedRef.current) return
      setStageLookup(false)
      const live = liveParamsRef.current
      if (stage === null || live.get(SCOPE_PARAM) !== scopeAtStart) return
      if (live.get(STAGE_PARAM) !== stageAtStart) return // the operator picked a stage meanwhile
      pendingViewRef.current = null
      pickStage({ stage, only: null }, { replace: true })
    })
  }, [railLocation.key, scope, boardProjectId, searchParams, taskTree, taskTreeFailed]) // eslint-disable-line react-hooks/exhaustive-deps
  // The lazy-load window: rows ASKED for, growing a page at a time via the sentinel and reset on filter change.
  // `shared.tsx`'s `buildTasksUrl` doc says why the window GROWS rather than stitching offset pages.
  const [limit, setLimit] = useState(TASK_PAGE_SIZE)
  // Expand state is persisted to `localStorage` under ADMIN-SCOPED keys (`lib/taskExpandStorage.ts` owns the
  // storage shape); this page owns only when to read and write.
  const can = useCan()
  const canReadWorktrees = can('worktrees:view')
  /* R5: the live worktree set, read after the list and re-read on the events that move it (below) so a freshly
     dispatched task links to its worktree; a failure keeps the prior set (no link, never a broken page). Keyed on
     the capability: the shell renders from the persisted permission snapshot, so `worktrees:view` may arrive
     late — and without it, one refused request per poll cycle forever. */
  const worktrees = useWorktrees({ enabled: canReadWorktrees && listLanded }).data ?? NO_WORKTREES
  // The three task WRITE keys: `tasks:view` implies none, so a rendered row or board is no evidence of any
  // write. `hooks/useTaskAccess.ts` single-homes which key covers which verb; hence one `undefined` per affordance.
  const canEditTasks = useCanEditTasks()
  const canOnProject = useCanOnProject()
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
  // Remembered expansions the board has not drawn yet (see the restore effect), and the restore's generation: an
  // admin switch mid-read discards the previous outline's answers.
  const pendingRestoreRef = useRef<Set<number>>(new Set())
  // …and those claimed by a restore read not yet applied (in flight, or failed for a reason that is not "gone"):
  // still remembered, and a user's own toggle meanwhile wins over the read's answer.
  const restoreHeldRef = useRef<Set<number>>(new Set())
  const restoreGenRef = useRef(0)
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

  function listUrl(size: number) {
    return buildTasksUrl({
      scopeFilter: listScope, showHidden, limit: size, bounds: dateBounds(), assigneeIds, focus, q, sources,
      ...windowShape, state: stateFilter, persona, creatorIds,
    })
  }

  /* A NEW SELECTION RESETS THE WINDOW, whoever moved it — the sidebar's (and the phone dropdown's) links navigate
     without `writeUrl`. Reset DURING RENDER, so the fetch sees the reset limit: one request per change.
     AND ITS ROWS: the last scope's rows under the new scope's section counts read as that scope's (a "Waiting 2"
     over no row) until the list landed. The new scope paints its own last first page (`lib/taskListSeed.ts`) at
     once, else the skeleton; a failed read then clears it, as on a reload. The left scope's tag totals and failure
     go with its rows. */
  const windowScopeKey = scopeKey(scope)
  /* A NEW SELECTION DROPS THE STAGE PICK (a stage id is one project's): THE REMEMBERED FILTERS, above, do it. */
  const [limitScopeKey, setLimitScopeKey] = useState(windowScopeKey)
  if (limitScopeKey !== windowScopeKey) {
    setLimitScopeKey(windowScopeKey)
    setLimit(TASK_PAGE_SIZE)
    const seeded = readTaskListSeed(queryClient, listUrl(TASK_PAGE_SIZE))
    setTasks(seeded ?? [])
    setLoading(seeded === undefined)
    setNextCursor(null)
    setError('')
    setErrorCause(null)
    paintedListUrlRef.current = null
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
      // The first window only: it is what a reload asks for first (`lib/taskListSeed.ts`).
      if (limit === TASK_PAGE_SIZE) saveTaskListSeed(queryClient, url, data, epoch)
      setNextCursor(next)
      setError('')
      setErrorCause(null)
      setLoading(false)
    } catch (err) {
      if (gen !== listFetchGenRef.current) return // stale failure — a newer fetch owns the state
      // Rows painted from the last visit's seed are not an answer: a refusal must not leave them on screen.
      if (paintedListUrlRef.current === null) setTasks([])
      setError('Failed to load tasks')
      setErrorCause(err)
    } finally {
      // Only the latest fetch clears the initial loading state.
      if (gen === listFetchGenRef.current) setLoading(false)
      setListLanded(true)
    }
  }

  /* THE API IS BACK after an outage that covered the list's failure: read again at once, rather than leave an empty
     board with no message until the stream reopens or the down-poll fires. Only a covered failure — one that went
     `down` — so a route's own 503 the probe answered never loops here. */
  const recovered = useRecoveries()
  useEffect(() => {
    if (recovered > 0 && outageCoversError) void fetchTasks()
  }, [recovered])

  /* A RELOAD PAINTS THE LAST FIRST PAGE while the list's own read is in flight (`lib/taskListSeed.ts`), once the
     persisted cache is restored and only while no answer has been painted. No cursor: the next page waits for the
     live answer's. */
  const restoringCache = useIsRestoring()
  useEffect(() => {
    if (restoringCache || paintedListUrlRef.current !== null) return
    const seeded = readTaskListSeed(queryClient, listUrl(limit))
    if (seeded === undefined) return
    setTasks(prev => (prev.length === 0 ? seeded : prev))
    setLoading(false)
  }, [restoringCache])

  // Refetch expanded parents' details so their rollups stay live — only those the board DRAWS
  // (`drawnExpandedIds` over `displayTasks`, the pinned row included): a remembered expansion out of the window
  // refreshes on the first event after it is drawn again. Only ever CALLED after render (events, polls), so
  // `displayTasks`, declared below, is initialised. Both guards needed (R6): the generation stamp, and
  // `id in prev` so a detail collapsed mid-fetch is never resurrected.
  async function refetchExpandedDetails() {
    const ids = drawnExpandedIds(displayTasks, expanded)
    if (ids.length === 0) return
    const gen = ++detailFetchGenRef.current
    await Promise.all(
      ids.map(async id => {
        try {
          const detail = await get<PipelineTask>(expandedDetailUrl(id))
          if (gen !== detailFetchGenRef.current) return // superseded batch — discard
          setExpanded(prev => (id in prev ? { ...prev, [id]: detail } : prev))
        } catch {
          if (gen !== detailFetchGenRef.current) return // stale failure — a newer batch owns the state
          setExpanded(prev => (id in prev ? { ...prev, [id]: { error: 'Failed to load task detail' } } : prev))
        }
      }),
    )
  }

  /* The feed's State totals (`feedStateCounts`) on the live/poll paths only: a filter change moves the query's
     key, which fetches by itself — invalidating there too would cancel and repeat that request. A read still in
     flight is JOINED (`cancelRefetch: false`), as the tree's: on a slow API every event cancelled the last one. */
  function refetchFeedStateCounts() {
    if (feedStateGrouped) void queryClient.invalidateQueries({ queryKey: [...PANE_QUERY_KEY, 'counts'] }, { cancelRefetch: false })
  }

  /* THE SECTION TOTALS COME FROM THE SERVER (`GET /api/tasks/tag-counts`): a count over the lazy 50-row window
     would lie. Through the shared builder (`shared.tsx`): totals must count the SAME row set as the list, and the
     counting route must NOT take `limit` (the window never changes what is counted). The URL is the key: every
     filter — `zone` included, else a total on yesterday's calendar sits beside today's rows — refetches by itself,
     the newest read wins, and a failure keeps the last-known totals. A TAG group only (recency sections rows
     already held): ungrouped, this would be a useless request per refetch and poll. `undefined` until it lands —
     no number rather than an unbacked zero; a filter change keeps the last totals meanwhile, a scope change does
     not (the left scope's go with its rows). */
  const tagCountsUrl = buildTagCountsUrl({
    scopeFilter: listScope, showHidden, bounds: dateBounds(), assigneeIds, focus, q, sources, state: stateFilter, persona, creatorIds,
  })
  const tagCounts = useQuery({
    queryKey: ['task-tag-counts', listScopeKey, tagCountsUrl],
    queryFn: () => get<TagCounts>(tagCountsUrl),
    enabled: isTagGroupBy(effectiveGroupBy),
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === listScopeKey ? previous : undefined),
    meta: { persist: false },
  }).data

  /* R5: the worktree set on the events that move it (below); its read and its guard are `useWorktrees` above. */
  function refetchWorktrees() {
    void queryClient.invalidateQueries({ queryKey: WORKTREES_KEY })
  }

  /* A CORRECTION of one URL pick against the vocabulary just fetched: never pushed (a response, not the operator,
     drove it) and never written when there is nothing to correct (an equal write is still a router commit, and
     the assignee one runs on every scope change). */
  function keepKnown(filter: 'assigneeIds' | 'creatorIds' | 'sources', known: ReadonlySet<string>): void {
    const base = liveParamsRef.current
    const current = readTaskUrlFilters(base)
    const kept = current[filter].filter(id => known.has(id))
    if (kept.length === current[filter].length) return
    const written = writeTaskUrlFilters({ ...current, [filter]: kept }, base)
    liveParamsRef.current = written
    setSearchParams(written, { replace: true })
  }

  // Initial load + refetch whenever a filter or the lazy window changes. `limit` is a dep (growing the window
  // IS a refetch), and `patchUrlFilters` resets it in the same batched render, so one request per change.
  // `assigneeKey`, not `assigneeIds` (see its declaration). `zone` because it is half of what the date window
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
    listScopeKey, showHidden, dateRange, dateFrom, dateTo, zone, dayStart, assigneeKey, creatorKey, sourceKey, focus, limit,
    q, stateFilter, persona, windowAfter, windowBefore, windowShape.sectionOrder, windowShape.excludeDone,
  ])

  /* CLEAR ALL — every badge-counted filter plus Search, the URL's in one push (so Back undoes them in one step).
     NOT Focus: it has no chip and no badge count, so clearing it was a change nothing on screen announced; its own
     toggle turns it off. */
  function clearFilters() {
    /* A picked bucket is the toolbar's Show filter (badge-counted): Clear drops it in the same push. A stage pick stays. */
    const bucket = isBucket(liveParamsRef.current.get(STAGE_PARAM))
    if (!hasTaskUrlFilters(urlFilters) && !bucket) return
    const base = new URLSearchParams(liveParamsRef.current)
    if (bucket) {
      base.delete(STAGE_PARAM)
      base.delete(ONLY_PARAM)
    }
    writeUrl(writeTaskUrlFilters({
      ...readTaskUrlFilters(base),
      showHidden: false, dateRange: 'all', assigneeIds: [], creatorIds: [], sources: [], state: '', q: '', persona: '', windowAfter: '', windowBefore: '',
    }, base))
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
     with none resolves to no bounds (`lib/taskDateRange.ts`), never an empty board. A Period pick REPLACES a link's
     exact window, which would otherwise win over it (`dateBounds`) and leave the pick doing nothing. */
  function changeDateRange(next: TaskDateRangeId) {
    patchUrlFilters({ dateRange: next, windowAfter: '', windowBefore: '' })
  }

  function changeDateFrom(next: string) {
    patchUrlFilters({ dateFrom: next, windowAfter: '', windowBefore: '' })
  }

  function changeDateTo(next: string) {
    patchUrlFilters({ dateTo: next, windowAfter: '', windowBefore: '' })
  }

  /* THE ROSTER, ORDERED ONCE — current user first (`lib/taskAssignees.ts`) — for both surfaces, so the eight-cap
     strip and the panel never disagree on who shows. */
  const orderedAssignees = useMemo(
    () => orderAssignees(assignees, admin?.id ?? null),
    [assignees, admin?.id],
  )

  /* One setter for both surfaces, so neither holds a pick the other cannot see. */
  function changeAssigneeIds(next: string[]) {
    patchUrlFilters({ assigneeIds: next })
  }

  /* The Creator roster in the same order rule (the caller first). */
  const orderedCreators = useMemo(() => orderAssignees(creators, admin?.id ?? null), [creators, admin?.id])

  function changeCreatorIds(next: string[]) {
    patchUrlFilters({ creatorIds: next })
  }

  function changeSources(next: string[]) {
    patchUrlFilters({ sources: next })
  }

  function changeStateFilter(next: string) {
    patchUrlFilters({ state: next })
  }

  /* Persona and the exact window arrive on a link (the command center's counts) and leave by their chips; no
     menu control sets them. */
  function clearPersona() {
    patchUrlFilters({ persona: '' })
  }

  function clearWindow() {
    patchUrlFilters({ windowAfter: '', windowBefore: '' })
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
    const value = groupParamValue(next, defaultGroupBy)
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

  // RESTORE the saved outline on mount and whenever the admin changes (`setExpanded({})` drops the previous
  // admin's rows). Only IDS are stored; they wait in `pendingRestoreRef` until the board DRAWS their row, and only
  // then is each REFETCHED (`restoreDrawnExpansions` below) — a remembered row off the window costs no read.
  // ABOVE the deep-link effect, so the reset lands before `revealTask` expands its ancestor.
  // The write-through gate is armed from a PROMISE CONTINUATION, never inline: the write-through effect runs in this
  // flush with OLD values and would save the previous admin's ids under the new key.
  useEffect(() => {
    let cancelled = false
    const gen = ++restoreGenRef.current
    setExpanded({})
    pendingRestoreRef.current = new Set(loadExpandedTaskIds(expandedStorageKey))
    restoreHeldRef.current = new Set()
    void Promise.resolve().then(() => {
      if (!cancelled && gen === restoreGenRef.current) restoredExpandedKeyRef.current = expandedStorageKey
    })
    return () => { cancelled = true }
  }, [expandedStorageKey])

  // WRITE THROUGH on changes to the expanded ID SET (ascending, stable `Object.keys`), so detail refetches
  // rewriting VALUES cost no storage write. Remembered ids not yet restored ride along FIRST, so the cap
  // (`MAX_STORED_EXPANDED_IDS`) drops them before a live expansion. The save's result is ignored: failing to
  // remember must never cost the expansion (`saveExpandedTaskIds` swallows blocked-storage throws).
  const expandedIdsKey = Object.keys(expanded).join(',')
  useEffect(() => {
    if (restoredExpandedKeyRef.current !== expandedStorageKey) return
    const held = Object.keys(expanded).map(Number)
    const waiting = [...pendingRestoreRef.current, ...restoreHeldRef.current].filter(id => !(id in expanded))
    saveExpandedTaskIds(expandedStorageKey, [...waiting, ...held])
  }, [expandedIdsKey, expandedStorageKey])

  // RE-restore the stored Focus when the bucket changes (a late admin id, or an in-place admin switch). A
  // NO-OP on mount (the ref holds the mount key), so the first fetch is the only fetch.
  // …AND IT CLEARS THE URL FILTERS: they are keyed on nobody, so the incoming operator would inherit the outgoing
  // one's assignee picks. Guarded, since an unconditional write is a router commit on every snapshot→server
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
    // Section totals ride the same events: a header must never disagree with its own rows (a filter change moves
    // the counts' key, which fetches by itself).
    void queryClient.invalidateQueries({ queryKey: ['task-tag-counts'] })
    refetchFeedStateCounts()
    void queryClient.invalidateQueries({ queryKey: PIPELINE_ALL_ITEMS_KEY })
  }, STREAM_DOWN_POLL_MS, { entity: 'task', minIntervalMs: TASKS_LIVE_REFETCH_MIN_MS })
  /* THE WORKTREE SET on what moves it — a slot's or a runner's event (a node's observation, a lease, a session
     starting) — not on every `task` event: a busy board re-read the whole pool per transition. Polls with the
     stream down, as the board does. */
  useLiveRefetch(useEventStream(), refetchWorktrees, STREAM_DOWN_POLL_MS, {
    match: event => WORKTREE_EVENT_ENTITIES.has(event.entity),
  })
  /* A task this viewer just opened loses its "not yet seen" halo here, with no re-read (`lib/taskSeen.ts`). */
  useEffect(() => {
    const seen = (event: Event) => {
      const id = (event as CustomEvent<number>).detail
      setTasks(prev => prev.map(t => (t.id === id && t.done_unseen ? { ...t, done_unseen: false } : t)))
    }
    window.addEventListener(TASK_SEEN, seen)
    return () => window.removeEventListener(TASK_SEEN, seen)
  }, [])
  /* A saved pin change re-reads the rows: the server orders the page pins-first (`lib/taskPin.ts`). */
  useEffect(() => {
    const repin = () => void latest.current.fetchTasks()
    window.addEventListener(TASK_PINS_SAVED, repin)
    return () => window.removeEventListener(TASK_PINS_SAVED, repin)
  }, [])
  /* …and, at most once per `WORKTREES_TASK_REFETCH_MIN_MS`, on `task` events: a control-plane lease or release
     (dispatch, Reset, Delete) emits only a `task` event, and a slot whose node saw nothing move emits no other. */
  useLiveRefetch(useEventStream(), refetchWorktrees, null, {
    entity: 'task', minIntervalMs: WORKTREES_TASK_REFETCH_MIN_MS,
  })
  /* A running task's `metered` usage (the Tokens/Cost cells) re-reads only the rows, at most once a minute: one
     event per runner every few minutes would otherwise re-read the whole board each time. The figures already
     trail by minutes, so the throttle costs nothing a reader could see. */
  useLiveRefetch(useEventStream(), () => {
    void fetchTasks()
    void refetchExpandedDetails()
  }, null, { entity: 'task', actions: ['metered'], minIntervalMs: METERED_REFETCH_MIN_MS })

  async function toggleExpand(id: number) {
    // The user's own toggle settles a remembered row: a restore read still in flight is dropped on arrival.
    restoreHeldRef.current.delete(id)
    pendingRestoreRef.current.delete(id)
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
      const detail = await get<PipelineTask>(expandedDetailUrl(id))
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
    discard: discardTask, preemptDialog,
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

  /* REASSIGN IN PLACE — the row's Creator → Assignee cell (`components/tasks/TaskPeople.tsx`). A partial `PUT`
     (the one door that validates the member, `_validated_assignee`); the answer replaces the row. Under an Assignee
     filter the row may no longer belong, and the totals moved: the board refetches instead. */
  function assignTask(task: PipelineTask, assigneeId: string) {
    void rowWrite(task, 'Failed to reassign task', async () => {
      const updated = await put<PipelineTask>(`/api/tasks/${task.id}`, { assigned_to_id: assigneeId })
      if (assigneeIds.length > 0) void fetchTasks()
      else replaceTaskEverywhere(updated)
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

  // Opens the dock console on a slot; a row passes only one already gated (`rowActions.tsx`). The request
  // (the server's `may_edit` verdict, Title, session snapshot) is `hooks/useWorktreeAccess.ts::slotConsoleRequest`.
  // This board gates the console on the TASK's state, so a dead runner's task still offers it; the pane
  // reads `hadLiveSession` to say "empty slot" rather than a glitch-like message.
  function openConsole(wt: Worktree) {
    openDockConsole(slotConsoleRequest(wt))
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
  /* A stage picked across projects draws that stage's tasks instead (`allStage`, above). */
  const stageTasks = allStageName === null ? null : allStage?.tasks ?? NO_TASKS
  const displayTasks = useMemo(
    () => stageTasks ?? (pinnedIsMerged && pinnedTask ? [pinnedTask, ...tasks] : tasks),
    [stageTasks, tasks, pinnedTask, pinnedIsMerged],
  )
  /* RESTORE WHAT THE BOARD DRAWS: each remembered id whose row is on screen (`drawnPendingIds` — top level, the
     pinned row, or a child of a drawn expansion, so a nested outline restores level by level) is claimed and read
     once. An id the server no longer shows (404/403) is forgotten; any other failure keeps it remembered for the
     next visit. An id off the window waits. */
  useEffect(() => {
    const pending = pendingRestoreRef.current
    if (pending.size === 0) return
    const ids = drawnPendingIds(displayTasks, expanded, pending)
    if (ids.length === 0) return
    const gen = restoreGenRef.current
    const held = restoreHeldRef.current
    for (const id of ids) {
      pending.delete(id)
      held.add(id)
    }
    for (const id of ids) {
      void get<PipelineTask>(expandedDetailUrl(id))
        .then(detail => {
          // Only while still held: a toggle by the user meanwhile (`toggleExpand`) has the last word. Merged, not
          // assigned: a deep link may have expanded another row meanwhile.
          if (gen !== restoreGenRef.current || !held.delete(id)) return
          setExpanded(prev => ({ ...prev, [id]: detail }))
        })
        .catch(error => {
          if (gen === restoreGenRef.current && error instanceof ApiError && (error.status === 404 || error.status === 403)) {
            held.delete(id)
          }
        })
    }
  }, [displayTasks, expanded])
  /* Another org's rows on the board (an operator's cross-org view) — every row drawn, the pinned one and an
     expanded epic's stage tasks included: each row's verbs — Start, Stop, Reset, Complete — go as
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
  /* Another org's pipeline: its step verbs (Release, Approve, Go…) go as the project's org, as Project settings'
     do, so the API answers them as the `can_*` flags it read (`app/releases/access.py::as_writer`). */
  /* The project's org from the tree, else the project roster; `undefined` while neither has the project yet, when the
     release step waits rather than offer a verb that would go unheld. */
  const pipelineOrgRef = pipelineProjectId === null ? null
    : (locateScope(scope, taskTree).project?.org ?? allProjects.find(p => p.id === pipelineProjectId)?.org)
  const pipelineOrg = pipelineOrgRef ? otherOrgOf(pipelineOrgRef) : null
  if (pipelineOrg) heldRows.push({ path: `/api/projects/${pipelineProjectId}`, org: pipelineOrg })
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
  // The sentinel (`hooks/useLoadMoreSentinel`) is re-armed each time `canLoadMore` turns true: a page landed.
  const sentinelRef = useLoadMoreSentinel(canLoadMore, loadNextPage)
  // The list's scroll region (`LIST_SCROLL_SX`): the rows' scrollport above the phone, a plain box on it.
  const listScrollRef = useRef<HTMLDivElement | null>(null)

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
    () => ({ showHidden, bounds: dateBounds(), assigneeIds, creatorIds, sources, state: stateFilter, persona, focus, q, muted }),
    [
      showHidden, dateRange, dateFrom, dateTo, windowAfter, windowBefore, zone, dayStart, assigneeKey, creatorKey, sourceKey,
      stateFilter, persona, focus, q, muted,
    ],
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
  const latest = useRef({ navigate, startTask, stopTask, resetTask, completeTask, deleteTask, discardTask, openConsole, unfoldSectionFor, fetchTasks, expandState, assignTask })
  latest.current = { navigate, startTask, stopTask, resetTask, completeTask, deleteTask, discardTask, openConsole, unfoldSectionFor, fetchTasks, expandState, assignTask }

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
    onDiscard: canDeleteTasks || canCreateTasks ? (task: PipelineTask) => void latest.current.discardTask(task) : undefined,
    onDuplicate: canCreateTasks
      ? (task: PipelineTask) => duplicateTask(task, otherOrgOf(task.org), latest.current.navigate)
      : undefined,
    canEditRow,
    canDeleteRow,
    canRunRow,
    /* A pending org starts nothing (the API 403s it): every Start renders disabled with the reason. */
    startBlockedReason: awaitingApproval,
    onAssign: canEditTasks ? (task: PipelineTask, id: string) => latest.current.assignTask(task, id) : undefined,
    /* Another org's row: a read-only note and Act as lead its ⋯; its Duplicate copies into that org. */
    otherOrgOf,
    worktreeFor,
    consoleWorktreeFor,
    onOpenConsole: (wt: Worktree) => latest.current.openConsole(wt),
    /* This user's pins (`hooks/useTaskPins.ts`); none on the Live Cast viewer, which has no user to keep them. */
    ...(viewer ? {} : { isPinned, onTogglePin }),
  }), [
    canEditTasks, canCreateTasks, canDeleteTasks, canEditRow, canDeleteRow, canRunRow, otherOrgOf, awaitingApproval,
    worktreeFor, consoleWorktreeFor, viewer, isPinned, onTogglePin,
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
     dock, never the board. The page re-renders when the pick empties or fills (`picking`: the dock
     follows it), and once more when the clicking pauses (`PICK_ECHO_DELAY_MS`, at low priority) so the
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
    [viewer, mayDelete, pickStore, isPickable],
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
      // Whole pixels, rounded up: a scroll position is quantised, so a fractional lift (a sub-pixel row height above
      // the list) rounded away and left the row under the dock.
      const by = Math.ceil(hidden)
      if (pane) pane.scrollTop += by
      else window.scrollBy(0, by)
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

  /* A pick this pipeline does not hold (another project's stage, a stale link) reads as All. */
  const pickHeld = pipelineView === undefined || stagePick.stage === null || isBucket(stagePick.stage) ||
    pipelineView.stages.some(stage => stage.id === stagePick.stage)
  const livePick: StripPick = pickHeld && stagePick.stage !== null ? stagePick : { stage: null, only: null }
  /* THE RELEASE STEP PICKED (`?only=release` on its source stage): the list is what waits there, with every release
     action atop it (`ReleasePick`) — a pick like any other, so another pick or Back leaves it. */
  const releaseFrom = pipelineProjectId !== null && livePick.only === RELEASE_ONLY ? livePick.stage : null

  return (
    <Box data-testid="tasks-page" {...{ [SHELL_FILL_ATTR]: '' }} sx={TASKS_PAGE_SX}>
      {/* THE STAGE STRIP, ABOVE SEARCH (AC-4): one project's stages, or the totals across projects. */}
      {pipelineProjectId !== null && (
        <Box sx={{ flexShrink: 0 }}>
          <ActingForNotice org={pipelineOrg} testId="pipeline-acting-for" />
          {pipelineView && (
            <StageStrip
              view={pipelineView}
              pick={livePick}
              onPick={pickStage}
              settingsHref={`/projects/${pipelineProjectId}?tab=customize&section=ci-pipeline`}
              projectId={pipelineProjectId}
              onRolledBack={() => {
                void queryClient.invalidateQueries({ queryKey: ['pipeline-view', pipelineProjectId] })
                refreshPipelineItems(queryClient, pipelineProjectId)
                void queryClient.invalidateQueries({ queryKey: releaseKey(pipelineProjectId) })
              }}
              /* The Release step's one mark; pressing it picks the step. */
              releaseSlot={(step, from) => pipelineOrgRef === undefined ? null : (
                <ReleaseStep
                  projectId={pipelineProjectId}
                  fromStageId={from.id}
                  toStageId={step.to_stage_id}
                  run={step.release?.run ?? null}
                  pressed={releaseFrom === from.id}
                  onRelease={fromId => pickStage({ stage: fromId, only: RELEASE_ONLY })}
                />
              )}
            />
          )}
        </Box>
      )}
      {/* Mounted before the totals answer, so the phone row holds its height and the header never drops (a failed
          read: no row). */}
      {allProjectsPipeline && (pipelineTotals || !pipelineTotalsQuery.isError) && (
        <Box sx={{ flexShrink: 0 }}>
          <StageTotals totals={pipelineTotals?.totals} unfiltered={isUnfiltered(pipelineFilters)} pick={allStageName}
            onPick={name => pickStage({ stage: name, only: null })} />
        </Box>
      )}
      <PaneHeader
        scope={scope}
        tree={taskTree}
        assignees={orderedAssignees}
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
          /* THE ASSIGNEE FILTER'S SECOND SURFACE — the same filter as the avatar strip (same roster, ids, "(you)",
             setter); the menu's Assignee page is the narrow-viewport path and what the badge counts. */
          assignees: orderedAssignees,
          currentUserId: admin?.id ?? null,
          assigneeIds,
          onAssigneeIdsChange: changeAssigneeIds,
          creators: orderedCreators,
          creatorIds,
          onCreatorIdsChange: changeCreatorIds,
          taskSources,
          sources,
          onSourcesChange: changeSources,
          stateFilter,
          onStateFilterChange: changeStateFilter,
          /* A project pipeline's buckets (Not started, Done, Archived) — the strip's stages are its default. */
          listing: pipelineProjectId !== null && pipelineView
            ? {
                value: isBucket(livePick.stage) ? livePick.stage : '',
                options: [
                  { value: '', label: 'Stages' },
                  ...BUCKETS.map(([id, label]) => ({ value: id, label: `${label} ${pipelineView[id]}` })),
                ],
                // Stages over a stage pick changes nothing: the pick is the strip's.
                onPick: (value: string) => { if (value || isBucket(livePick.stage)) pickStage({ stage: value || null, only: null }) },
              }
            : undefined,
          persona,
          onPersonaClear: clearPersona,
          windowAfter,
          windowBefore,
          onWindowClear: clearWindow,
          groupBy: effectiveGroupBy,
          onGroupByChange: changeGroupBy,
          groupByOptions,
          columns: offeredColumns,
          hiddenColumns,
          onToggleColumn: viewer
            ? undefined
            : (key: string) =>
                void updatePreferences(prefs => ({ task_hidden_columns: toggledHiddenColumns(readHiddenColumns(prefs), key) })),
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

      {error && !outageCoversError && <Banner severity="error">{error}</Banner>}
      {pipelineProjectId !== null && <FrozenNowBanner view={pipelineView} />}
      <QueuedOverCap />
      {actionError && (
        <Banner severity="error" testId="task-action-error" onDismiss={() => setActionError(null)}>
          {actionError.text}
        </Banner>
      )}

      {confirmDialog}

      {/* THE PREEMPT DIALOG, PAGE-mounted like `confirmDialog` (`useTaskActions`). */}
      {preemptDialog}

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

      {pipelineProjectId !== null && releaseFrom !== null ? (
        <ReleasePick
          key={releaseFrom}
          projectId={pipelineProjectId}
          fromStageId={releaseFrom}
          runOf={toStageId => releaseRunTo(pipelineView, toStageId)}
          stepOf={toStageId => pipelineView?.steps.find(s => s.to_stage_id === toStageId)}
          arrivalOf={stageId => pipelineView?.stages.find(s => s.id === stageId)?.arrival}
          reasons={stepFrom(pipelineView, releaseFrom)?.reasons}
          canEdit={!viewer && canOnProject('projects:edit', pipelineProjectId)}
          customPipeline={allProjects.find(p => p.id === pipelineProjectId)?.pipeline_mode === 'custom'}
          filters={pipelineFilters}
          /* The pick stays: its list turns into the deploy in progress (`ReleasePick`). */
          onDone={() => {
            void queryClient.invalidateQueries({ queryKey: releaseKey(pipelineProjectId) })
            void queryClient.invalidateQueries({ queryKey: ['pipeline-view', pipelineProjectId] })
            refreshPipelineItems(queryClient, pipelineProjectId)
          }}
          listSx={LIST_SCROLL_SX}
          tree={taskTree}
          actions={rowActions}
          submitting={submitting}
          expanded={expanded}
          fetching={fetching}
          focus={focus}
          onToggleTask={id => void toggleExpand(id)}
          deepLinkedId={deepLinkedId}
          hiddenColumns={hiddenColumnSet}
        />
      ) : (<>
      {/* THE LIST'S OWN SCROLL REGION above the phone (`LIST_SCROLL_SX`); on the phone a plain box in `.app-main`'s
          scroll. Everything that scrolls with the rows sits in it; the header above and the dock below do not. */}
      <Box
        ref={listScrollRef}
        data-testid="tasks-list-scroll"
        sx={LIST_SCROLL_SX}
      >
      {/* The selected feature's description, whichever style draws its rows. */}
      {scope.kind === 'feature' && <FeatureDescription featureId={scope.featureId} />}

      {/* Explains the pinned-in epic row above a filtered-looking list. */}
      {pipelineProjectId === null && pinnedIsMerged && pinnedTask && (
        <Typography variant="caption" component="div" role="status" data-testid="list-truncation-note" sx={NOTE_SX}>
          Task #{pinnedTask.id} is outside the loaded list — pinned to the top.
        </Typography>
      )}

      {/* R4: never truncate silently — only when the window hit the server's ceiling with rows behind it; points at
          the filters. */}
      {pipelineProjectId === null && allStageName === null && !loading && limit >= TASK_LIST_MAX && nextCursor !== null && (
        <Typography variant="caption" component="div" role="status" data-testid="list-truncation-note" sx={NOTE_SX}>
          Showing the first {TASK_LIST_MAX} tasks — some may be hidden. Pick a project to narrow the
          list.
        </Typography>
      )}
      {allStageName !== null && allStage !== undefined && allStage.left > 0 && (
        <Typography variant="caption" component="div" role="status" data-testid="list-truncation-note" sx={NOTE_SX}>
          {allStage.left} more in {allStageName} — open a project to see them all.
        </Typography>
      )}
      {allStageName !== null && allStageQuery.isError && <Banner severity="error">Failed to load this stage</Banner>}

      {/* A stage picked across projects, its totals not read yet: never the whole list in the meantime. */}
      {stageLookup || (allProjectsPipeline && stagePick.stage !== null && pipelineTotals === undefined &&
        !pipelineTotalsQuery.isError) ? (
        <ActivityFeedView tasks={NO_TASKS} loading />
      ) : pipelineProjectId !== null ? (
        <PipelineSections
          projectId={pipelineProjectId}
          view={pipelineView}
          stepVerbs={pipelineOrgRef !== undefined}
          stage={livePick.stage}
          only={livePick.only}
          filters={pipelineFilters}
          groupBy={effectiveGroupBy}
          grouping={grouping}
          onToggleSection={toggleSection}
          tree={taskTree}
          actions={rowActions}
          submitting={submitting}
          expanded={expanded}
          fetching={fetching}
          focus={focus}
          filtered={hasTaskUrlFilters({ ...urlFilters, showHidden: false }) || focus}
          onToggleTask={id => void toggleExpand(id)}
          deepLinkedId={deepLinkedId}
          hiddenColumns={hiddenColumnSet}
          create={listCreate}
          canEdit={!viewer && canEditTasks}
          canDelete={!viewer && canDeleteTasks}
          confirm={confirm}
          onChanged={() => void fetchTasks()}
        />
      ) : (<>
      <ActivityFeedView
        tasks={displayTasks}
        /* A failed stage read shows its banner, never an empty list that reads as "nothing there". */
        loading={allStageName === null ? loading : allStageQuery.isPending || allStageQuery.isError}
        deepLinkedId={deepLinkedId}
        groupBy={effectiveGroupBy}
        /* THE HOST'S MEMOIZED OBJECT, never a fresh literal: the feed memoizes sectioning on it and the recency cut
           reads `Date.now()`. The section toggle rides as its own prop. */
        grouping={grouping}
        onToggleSection={toggleSection}
        /* A picked stage's tasks are not the feed's: its State totals would not count them. */
        stateGrouping={allStageName === null || !stateGrouping ? stateGrouping : { ...stateGrouping, counts: undefined }}
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
        hiddenColumns={hiddenColumnSet}
      />
      </>)}

      {/* Under the unfiltered board, a young org (at most `STARTER_MAX_TASKS` tasks, which `TemplateStarters` checks
          against the org-wide count itself; more rows on screen already rule it out) gets the setup diagram, then the
          Most popular templates; any filter keeps the ordinary list. */}
      {pipelineProjectId === null && allStageName === null && !loading && displayTasks.length <= STARTER_MAX_TASKS && !hasTaskUrlFilters({ ...urlFilters, showHidden: false }) && !focus && (
        <Suspense fallback={null}>
          <TemplateStarters />
        </Suspense>
      )}

      {/* THE LAZY-LOAD SENTINEL: a zero-height div after the list; in view (with 400px of runway) the window grows a
          page. Rendered only while more is plausible. A div, not the last row: the list must not carry this page's
          fetch policy. `aria-hidden` + `role="presentation"`. */}
      {canLoadMore && pipelineProjectId === null && allStageName === null && (
        <Box
          ref={sentinelRef}
          data-testid="tasks-load-more-sentinel"
          aria-hidden="true"
          role="presentation"
          sx={{ height: 1 }}
        />
      )}
      </Box>
      </>)}

      {/* THE BULK BAR, STUCK TO THE BOTTOM of the scrollport: drafts sit low on a State board, and a bar above the
          list would be out of sight while the operator picks them. Marked as a bottom bar so the Help launcher
          sits level with it, past its right end (`FORM_ACTION_BAR_ATTR`). */}
      {pipelineProjectId === null && (bulkDockShown || pickNote !== null) && (
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
