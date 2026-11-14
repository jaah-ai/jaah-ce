/* THE RELEASE STEP'S LIST (`?stage=<source stage id>&only=release`, `pages/Tasks.tsx`) — the strip's Release mark
 * picks it (`ReleaseStep.tsx`) as any strip pick, under the page's own header row: what waits on the source stage as
 * the board's own rows (`ActivityFeedView`, read from the stage's items), none ticked; a waiting change with no task is
 * a plain line with its own tick. ONE ROW ATOP THE LIST, figures each with its words on hover:
 * tick-all and shipping/listed (over the rows the list shows, as the source stage's block counts them; both absent
 * while it shows none) — Release N names the same number —, the changes not listed here (their task filtered out or
 * on another stage; only while everything ships), the extra commits (only while everything ships: a pick ships its
 * changes alone), the shipping tasks' cost and tokens, the oldest shipping change's age, then EVERY RELEASE ACTION,
 * each shown only while the server offers it (`can_*`, `next_action`): a failure's red mark (where and why on hover)
 * with Retry, Run or Connect deploys; When (a clock reading Now, pressed for a date and time) and Release N; Hotfix;
 * Connect deploys; Check what is waiting. No ✕ (another pick leaves it), no menu, no Schedule button. Nothing ticked
 * — or everything — releases the stage whole (`deploy` without `change_ids`, the extra commits too); a pick sends its changes (`change_ids`, a `pick`
 * release). A frozen target opens When at the freeze's end. Refusals read through `refusal`.
 * WITH THE LANE ON, a whole promote ships the lane's newest tested commit: the changes merged after it never count or
 * list as shipping; they sit below, unticked, under "Waiting for tests · N" (`promote_pin.newer_change_ids`), and
 * Release untested ships them too, the head without the full tests, on a typed reason (`untested_reason`). Release
 * itself stands at all times: disabled when it cannot send, why on hover.
 * A RELEASE IN PROGRESS TAKES THE LIST OVER — picked from the strip's turning Release mark, or by Release here: the
 * row says its state with a running progress line, Approve, Open nodes, Run and Cancel where offered, and the list
 * is the release as Production shows one (`ReleaseFoldRow`: the new version, its tasks by feature). Once it lands
 * the row says how it ended; the line stops. */
import { type JSX, useEffect, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import IconButton from '@mui/material/IconButton'
import InputAdornment from '@mui/material/InputAdornment'
import LinearProgress from '@mui/material/LinearProgress'
import Link from '@mui/material/Link'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'
import { useSubmit } from '../../hooks/useSubmit'
import { compactCount, countOf, groupedCountOf } from '../../lib/humanize'
import { clearPicks, createPickStore, togglePick, usePicks } from '../../lib/pickStore'
import {
  freezeHolding, getPipelineItemsPage, isUnfiltered, pipelineItemsKey, type ItemFilters, type PipelineItem,
  type ReleaseParts, type ReleaseRun, type StageArrival, type ViewStep, type WaitReason,
} from '../../lib/pipelineBoard'
import ArrivalChip, { arrivalFailed } from './ArrivalChip'
import { WaitNote } from './WaitChip'
import {
  approve, cancel, changeRow, deploy, deployLabel, fromLocalInput, getReleaseTab, inFlightLabel, openInto, releaseHistory,
  releaseKey, RELEASE_POLL_MS, reject, requestSurvey, retry, stallLine, stallWhy, STATUS, statusOfRelease, taskCount, toLocalInput,
  type Change, type PromotePin, type Release,
} from '../../lib/releases'
import { safeHref } from '../../lib/safeHref'
import { compactAge, shortDate } from '../../lib/relativeTime'
import { costLabel, formatCostUsd, hasCost, parseCostUsd } from '../../lib/taskCost'
import { hasTokens } from '../../lib/taskTokens'
import { GROUP_BY_NONE } from '../../lib/taskGrouping'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, SIZE, grid } from '../../theme'
import type { PipelineTask } from '../../types'
import { RAIL_CENTRED_SX, rowPadLeft } from '../../ui/feed/geometry'
import { ClockIcon, ErrorCircleIcon, RefreshIcon } from '../../ui/icons'
import { PHONE, SELECT_BOX_SX } from '../../ui/listStyles'
import Tip from '../../ui/Tip'
import ActivityFeedView from '../tasks/listViews/ActivityFeedView'
import type { PaneSelection, TaskListViewProps } from '../tasks/listViews/types'
import StatusChip from '../../ui/StatusChip'
import ApprovalDialog, { type ApprovalMode } from './ApprovalDialog'
import DeployDialog, { refusal, type Pending } from './DeployDialog'
import { GateResults } from './gates/GateResults'
import TicketField from './TicketField'
import OverrideFreezeDialog from './OverrideFreezeDialog'
import { ReleaseFoldRow } from './ReleaseList'
import { failedWhere, releaseStepOf } from './ReleaseStep'

const NO_FILTERS: ItemFilters = {}

/** A deploy that is under way (the progress line runs); scheduled or awaiting approval is not. */
const RUNNING: ReadonlySet<string> = new Set(['queued', 'merging', 'deploying'])
/** How long Release waits for its release to show as open before the view falls back to the pick. */
const STARTING_MAX_MS = 60_000

/** The most item pages one release read takes (`PAGE_LIMIT` each): a stage's waiting work, never its history. */
const MAX_PAGES = 10

/* The header's row, drawn as the feed's create row is: the tick on the rows' rail, so it heads their column. */
/* A gate result table under the bar: the list's own inset, never full-bleed. */
const GATES_SX = { px: 1, py: 1, '&:empty': { display: 'none' } } as const

const BAR_SX = {
  position: 'relative',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  minHeight: grid(5.5),
  ...rowPadLeft(0),
  pr: 0.5,
  mb: 0.5,
  borderBottom: '1px solid',
  borderColor: 'divider',
  /* On the phone the bar is two lines: the tick and the figures, then the actions on their own line, right-aligned,
     wrapping if they must — every action stays on screen. */
  [PHONE]: { ...rowPadLeft(0)[PHONE], gap: 0.5, flexWrap: 'wrap', alignItems: 'flex-start' },
} as const
/* Nothing to pick: no tick column, so the bar starts at the list's own inset. */
const BAR_BARE_SX = { ...BAR_SX, pl: 1, [PHONE]: { ...BAR_SX[PHONE], pl: 1 } } as const
/* The tick heads the figures' line, not the middle of a two-line bar. */
const BAR_TICK_SX = { ...RAIL_CENTRED_SX, [PHONE]: { ...RAIL_CENTRED_SX[PHONE], top: `calc(${grid(5.5)} / 2)` } } as const
/* The figures give way first: clipped, never wrapped onto a second line. */
const STATS_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  display: 'flex',
  alignItems: 'center',
  gap: 1.5,
  overflow: 'hidden',
  whiteSpace: 'nowrap',
  fontSize: FONT_SIZE.sm,
  color: 'text.secondary',
  fontVariantNumeric: 'tabular-nums',
  /* With no figures (nothing waiting), the phone's figure line goes: the actions take the bar alone. */
  [PHONE]: { gap: 1, minHeight: grid(5.5), '&:empty': { display: 'none' } },
} as const
/* The release in progress: its state, target and checks; on the phone they wrap rather than clip the checks away. */
const PROGRESS_STATS_SX = {
  ...STATS_SX, color: 'text.primary',
  [PHONE]: { ...STATS_SX[PHONE], flexWrap: 'wrap', whiteSpace: 'normal', rowGap: 0.5, py: 0.5 },
} as const
const COUNT_SX = { color: 'text.primary', fontSize: FONT_SIZE.md } as const
const ACTIONS_SX = {
  flex: 'none', display: 'flex', alignItems: 'center', gap: 0.5,
  [PHONE]: { flex: '1 0 100%', flexWrap: 'wrap', justifyContent: 'flex-end', pb: 0.5 },
} as const
const BTN_SX = { minWidth: 0, px: 1, py: 0, whiteSpace: 'nowrap', [PHONE]: { minHeight: SIZE.thumb } } as const
const WHEN_SX = { width: grid(25), '& input': { py: 0.5, fontSize: FONT_SIZE.sm } } as const
const CHANGE_ROW_SX = {
  position: 'relative',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  minHeight: grid(4),
  ...rowPadLeft(0),
  fontSize: FONT_SIZE.md,
  borderBottom: '1px solid',
  borderColor: 'divider',
} as const
/* The untested changes' heading: a quiet section label between the shipping list and theirs. */
const UNTESTED_HEADING_SX = {
  m: 0, px: 1, pt: 2, pb: 0.5, fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary',
  borderBottom: '1px solid', borderColor: 'divider',
} as const
/* Why the untested changes wait: one quiet line under their heading. */
const STALL_SX = { px: 1, py: 0.5, fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.snug, color: 'text.secondary' } as const
/* An untested change with no task row: no tick, so its words start where an untickable feed row's do. */
const UNTESTED_ROW_SX = { ...CHANGE_ROW_SX, pl: 1, [PHONE]: rowPadLeft(0)[PHONE] } as const
const NOTE_SX = { fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.snug, color: 'error.main', pb: 0.5 } as const

const WRAP_SX = { fontSize: FONT_SIZE.sm, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' } as const
const APPROVALS_SX = { listStyle: 'none', m: 0, pl: 1, pb: 0.5, display: 'flex', flexDirection: 'column', gap: 0.5 } as const

/** How a canceled release ended, in one line: only a timeout or a rejection says anything. */
function cancellationWords(c: NonNullable<Release['cancellation']>): string | null {
  if (c.kind === 'timeout') return 'Approval timed out'
  if (c.kind !== 'rejected') return null
  const by = c.by ? `Rejected by ${c.by.name}` : 'Rejected'
  return c.comment ? `${by} \u2014 ${c.comment}` : by
}

/** The epics the changes merge children of, each with those children: what an epic's row ships. */
export function partsOf(changes: readonly Change[]): ReleaseParts {
  const parts = new Map<number, number[]>()
  for (const change of changes) {
    const root = change.root_task_id
    if (root == null || change.task_id === null || change.task_id === root) continue
    const children = parts.get(root) ?? []
    if (!children.includes(change.task_id)) children.push(change.task_id)
    parts.set(root, children)
  }
  return parts
}

/** A figure on the bar, named by a word beside it ("$27 cost"); its full words on hover. */
function Stat({ testId, tip, children }: { testId: string; tip: string; children: string }): JSX.Element {
  return (
    <Tip title={tip} describeChild={false}>
      <Box component="span" tabIndex={0} aria-label={tip} data-testid={testId}>{children}</Box>
    </Tip>
  )
}

/** The commit a whole promote ships (`PromotePin`): its short sha, and how many merges behind the branch it is. */
function PinStat({ pin }: { pin: PromotePin }): JSX.Element {
  if (!pin.sha) {
    return <Stat testId="release-pick-pin" tip="No tested commit yet: the release tests the newest one first">Untested</Stat>
  }
  const short = pin.sha.slice(0, 7)
  /* The changes waiting apart below when the server names them, so the figure matches that list; else lane merges. */
  const behind = pin.newer_change_ids?.length ?? pin.behind ?? 0
  const age = pin.certified_at ? compactAge(pin.certified_at) : null
  const tested = age ? `, tested ${age === 'now' ? 'just now' : `${age} ago`}` : ''
  const stall = pin.stall ? `: ${stallWhy(pin.stall)}` : ''
  const tip = `Ships ${short}${tested}${behind > 0 ? `; ${countOf(behind, 'newer change')} not tested yet${stall}` : ''}`
  return <Stat testId="release-pick-pin" tip={tip}>{behind > 0 ? `${short} · ${behind} newer not tested yet` : short}</Stat>
}

/** A change's identity among the stage's taskless rows (`ItemChange` carries no change id). */
const lineKey = (title: string, mergedAt: string | null) => `${title}\u0000${mergedAt}`

/** The stage's rows as the list reads them: its tasks by id, and its taskless changes (`lineKey`). */
type StageRows = { byId: ReadonlyMap<number, PipelineTask>; lines: ReadonlySet<string> }

/** `changes` as the list draws them: the board's rows of their tasks (each once, in the changes' order; a change by
 *  its top-level task, so an epic's child's merge is the epic's row), plain lines for taskless changes, and the rest
 *  (`hidden`: neither a row nor a line, never counted as listed) — a change whose task's row sits on another stage
 *  (an epic still building) or that the header's filters leave out. A filter keeps a taskless change only where the
 *  stage still lists it (`board.py::Board.load` drops it under a task-only filter or a search its title misses). So
 *  the rows are the source stage's own, and the list counts as its block on the strip does. Until the rows land
 *  (`loaded`), nothing is hidden: the figures never flash 0/0. */
function rowsOf(changes: readonly Change[], stage: StageRows, narrowed: boolean, loaded: boolean) {
  const tasks: PipelineTask[] = []
  const plain: Change[] = []
  const hidden: Change[] = []
  for (const change of changes) {
    const owner = changeRow(change)
    const task = owner !== null ? stage.byId.get(owner) : undefined
    if (task) { if (!tasks.includes(task)) tasks.push(task) }
    else if (!loaded || (owner === null && (!narrowed || stage.lines.has(lineKey(change.title, change.merged_at))))) {
      plain.push(change)
    } else hidden.push(change)
  }
  return { tasks, plain, hidden }
}

/** Every page of the stage's items: the rows its waiting changes' tasks draw as. */
async function stageRows(projectId: number, stageId: string, filters: ItemFilters, signal: AbortSignal): Promise<PipelineItem[]> {
  const rows: PipelineItem[] = []
  let after: string | null = null
  for (let page = 0; page < MAX_PAGES; page++) {
    const read = await getPipelineItemsPage(projectId, { stage: stageId, group: null }, filters, after, signal)
    rows.push(...read.rows)
    after = read.next
    if (after === null) break
  }
  return rows
}

export type ReleasePickProps = Pick<
  TaskListViewProps,
  'tree' | 'actions' | 'submitting' | 'expanded' | 'fetching' | 'focus' | 'onToggleTask' | 'deepLinkedId' | 'hiddenColumns'
> & {
  projectId: number
  /** The release's source stage (`?stage=` with `?only=release`). */
  fromStageId: string
  /** A release step's run as the pipeline view reports it, by its target stage (where a failed one failed). */
  runOf: (toStageId: string) => ReleaseRun | null
  /** A release step as the pipeline view reports it, by its target stage (the freeze holding it, `can.override`). */
  stepOf?: (toStageId: string) => ViewStep | undefined
  /** A stage's newest arrival as the pipeline view reports it: the target's chip, its report when it failed. */
  arrivalOf?: (stageId: string) => StageArrival | null | undefined
  /** May edit the project: Connect is offered. */
  canEdit: boolean
  /** The project runs a custom pipeline: Connect opens its Production stage on Pipeline, else Customize › CI pipeline. */
  customPipeline?: boolean
  /** The page header's search and filters: they narrow the rows shown, never what an untouched pick ships. */
  filters?: ItemFilters
  /** After the deploy (or schedule) landed. */
  onDone: () => void
  /** The list's scroll region (the page's). */
  listSx?: object
  /** Why the release step waits (the view's `reasons`): its chip atop the list (`WaitChip.tsx`). */
  reasons?: WaitReason[]
}

export default function ReleasePick(props: ReleasePickProps): JSX.Element {
  /* What every list here hands the board's rows unchanged. */
  const feedProps = {
    deepLinkedId: props.deepLinkedId, groupBy: GROUP_BY_NONE, tree: props.tree, actions: props.actions,
    submitting: props.submitting, expanded: props.expanded, fetching: props.fetching, focus: props.focus,
    onToggleTask: props.onToggleTask, hiddenColumns: props.hiddenColumns,
  }
  const { projectId, fromStageId, onDone, filters = NO_FILTERS } = props
  const narrowed = !isUnfiltered(filters)
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  /* The deploy this view watches: the open one, then — once it lands — how it ended (the history's row). */
  const [watched, setWatched] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const tab = useQuery({
    queryKey: releaseKey(projectId),
    queryFn: () => getReleaseTab(projectId),
    refetchInterval: query => ((query.state.data?.open.length ?? 0) > 0 || starting ? RELEASE_POLL_MS : false),
  })
  const items = useQuery({
    queryKey: [...pipelineItemsKey(projectId), 'release', fromStageId, JSON.stringify(filters)],
    queryFn: ({ signal }) => stageRows(projectId, fromStageId, filters, signal),
    meta: { persist: false },
  })

  const data = tab.data
  const fromIndex = data ? data.stages.findIndex(s => s.id === fromStageId) : -1
  const from = fromIndex >= 0 ? data?.stages[fromIndex] : undefined
  const to = fromIndex >= 0 ? data?.stages[fromIndex + 1] : undefined
  const waiting: Change[] = useMemo(() => (from ? data?.waiting[from.id] ?? [] : []), [data, from])
  /* With the lane on, a whole promote out of the integration branch ships the lane's newest tested commit
     (`promote_pin`), not the branch's head: the changes merged after it are not in it, so they never count or list as
     shipping — they wait apart, under their own heading, for a later release. */
  const lanePin = from && data?.promote_pin?.stage_id === from.id ? data.promote_pin : null
  const { changes, untested } = useMemo(() => {
    const newer = new Set(lanePin?.sha ? lanePin.newer_change_ids ?? [] : [])
    return { changes: waiting.filter(c => !newer.has(c.change_id)), untested: waiting.filter(c => newer.has(c.change_id)) }
  }, [waiting, lanePin])
  const extra = from ? data?.extra_commits[from.id] ?? 0 : 0
  const open = to && data ? openInto(data.open, to.id) : undefined
  /* The last release to the target, for a failure's mark and its Retry. */
  const history = useQuery({
    queryKey: [...releaseKey(projectId), 'history', open ? `${open.id}:${open.state}` : ''],
    queryFn: () => releaseHistory(projectId),
    enabled: (data?.stages.length ?? 0) > 0,
    placeholderData: previous => previous,
  })
  const step = data && from && to ? releaseStepOf(data, history.data?.items ?? [], from.id, to.id) : null
  const failed = step?.failed
  useEffect(() => {
    if (open) {
      setWatched(open.id)
      setStarting(false)
    }
  }, [open?.id]) // eslint-disable-line react-hooks/exhaustive-deps
  /* A release that ends before a read shows it open: the wait gives up, and the view falls back to the pick. */
  useEffect(() => {
    if (!starting) return
    const timer = setTimeout(() => setStarting(false), STARTING_MAX_MS)
    return () => clearTimeout(timer)
  }, [starting])
  const ended = useQuery({
    queryKey: [...releaseKey(projectId), 'history', 'watched', watched, open?.state ?? 'closed'],
    queryFn: () => releaseHistory(projectId),
    enabled: watched !== null && !open,
    meta: { persist: false },
  }).data?.items.find(r => r.id === watched)
  const shown: Release | undefined = open ?? ended
  /* The shipping changes as the board's rows and plain lines (`rowsOf`); one the header's filters hide still ships
     while nothing is ticked (`hidden`). */
  const stageRowsRead = useMemo((): StageRows => {
    const byId = new Map<number, PipelineTask>()
    const lines = new Set<string>()
    for (const item of items.data ?? []) {
      if (item.task) byId.set(item.task.id, { ...item.task, pipeline_group: item.group, pipeline_stage: item.stage_id })
      else if (item.change) lines.add(lineKey(item.change.title, item.change.merged_at))
    }
    return { byId, lines }
  }, [items.data])
  const { tasks, plain, hidden } = useMemo(() => rowsOf(changes, stageRowsRead, narrowed, !!items.data),
    [stageRowsRead, changes, narrowed, items.data])
  /* The untested changes' own rows: an epic with a child on each side draws in both lists, each row its own children. */
  const later = useMemo(() => rowsOf(untested, stageRowsRead, narrowed, !!items.data), [untested, stageRowsRead, narrowed, items.data])
  const laterParts = useMemo(() => partsOf(untested), [untested])
  const laterShown = untested.length - later.hidden.length
  const listed = useMemo(() => new Set(tasks.map(t => t.id)), [tasks])
  const parts = useMemo(() => partsOf(changes), [changes])
  /* What the list shows: the figures count these, so they match the rows on screen. */
  const visible = useMemo(() => {
    const left = new Set(hidden)
    return changes.filter(c => !left.has(c))
  }, [changes, hidden])

  /* ── THE PICK ── the board's own ticks, none at first: nothing ticked ships everything, a tick ships only what is
     ticked. */
  const [store] = useState(() => createPickStore<PipelineTask>())
  const picks = usePicks(store)
  const [plainOn, setPlainOn] = useState<ReadonlySet<string>>(() => new Set())
  const [selection] = useState<PaneSelection>(() => ({
    store,
    isSelectable: () => true,
    onToggle: task => togglePick(store, task.id, task),
    onDrawn: () => {},
  }))
  const ticked = visible.filter(c => {
    const owner = changeRow(c)
    return owner !== null && listed.has(owner) ? picks.has(owner) : plainOn.has(c.change_id)
  })
  /* Ticks whose changes all left (shipped by someone else) ship nothing, never everything. */
  const anyTicks = picks.size > 0 || plainOn.size > 0
  const pickedChanges = anyTicks ? ticked : changes
  const all = pickedChanges.length === changes.length
  /* THE COUNT is of rows — a task once however many merges it made, a taskless change alone — so it matches the
     source stage's count on the strip; the changes elsewhere are their own figure (`hidden`). Until the rows land (or
     when they fail to), the waiting changes' tasks as the board would list them. */
  const rowCount = items.data ? tasks.length + plain.length : taskCount(changes)
  const tickedRows = tasks.filter(t => picks.has(t.id)).length + plain.filter(c => plainOn.has(c.change_id)).length
  /* Nothing listed to pick: no tick-all and no count — never an empty box beside "0/0". */
  const pickable = rowCount > 0
  /* The bar's count: of the rows, those that ship. */
  const shippingShown = anyTicks ? tickedRows : rowCount
  const tickAll = (on: boolean) => {
    if (on) {
      store.update(() => new Map(tasks.map(t => [t.id, t])))
      setPlainOn(new Set(plain.map(c => c.change_id)))
    } else {
      clearPicks(store)
      setPlainOn(new Set())
    }
  }
  const togglePlain = (id: string) => setPlainOn(prev => {
    const next = new Set(prev)
    if (!next.delete(id)) next.add(id)
    return next
  })

  /* ── THE FIGURES ── of the shipping tasks (each once) and changes. */
  const pickedTasks = anyTicks ? tasks.filter(t => picks.has(t.id)) : tasks
  const costed = pickedTasks.filter(t => hasCost(t))
  const cost = costed.reduce((sum, t) => sum + (parseCostUsd(t.cost_usd) ?? 0), 0)
  const tokened = pickedTasks.filter(t => hasTokens(t))
  const tokens = tokened.reduce((sum, t) => sum + (t.tokens_total ?? 0), 0)
  const oldest = pickedChanges.reduce<string | null>(
    (min, c) => (c.merged_at && (min === null || c.merged_at < min) ? c.merged_at : min), null)
  const extraShips = all && extra > 0
  /* A whole promote's pinned commit: which one, and how far behind the branch it is. A pick ships its own changes. */
  const pin = all ? lanePin : null
  const hiddenShips = !anyTicks && hidden.length > 0

  /* ── THE WRITE ── */
  const { busy, formError, run } = useSubmit()
  const verb = to ? deployLabel(to) : 'Release'
  const [scheduling, setScheduling] = useState(!!to?.frozen_until)
  const [when, setWhen] = useState(() => toLocalInput(to?.frozen_until ?? null))
  /* The tab may land after the first paint: a frozen target still opens scheduling, at the freeze's end. */
  const frozenUntil = to?.frozen_until ?? null
  useEffect(() => {
    if (!frozenUntil) return
    setScheduling(true)
    setWhen(prev => prev || toLocalInput(frozenUntil))
  }, [frozenUntil])
  const scheduledFor = scheduling ? fromLocalInput(when) : null
  /* Release's number is the bar's: the rows that ship. What ships beside them (changes elsewhere, extra commits) is the
     bar's "+N". */
  const shipCount = shippingShown
  const shipsAny = anyTicks ? pickedChanges.length > 0 : changes.length > 0 || extra > 0
  /* When stands while the source has anything tested to send; a pick of nothing only disables Release. */
  const mayPress = from?.next_action === 'deploy' && !open
  const releasable = mayPress && (changes.length > 0 || extra > 0)
  /* Release untested: the whole source, head and all, without the full tests, on a typed reason (`untested_reason`).
     Offered with the lane on while changes wait for tests: behind its newest tested commit, or all of them when it
     tested none under the current gate. Its count is what it ships. */
  const untestedCount = lanePin ? (lanePin.sha ? untested.length : waiting.length) : 0
  const untestedReady = mayPress && untestedCount > 0
  /* A step whose ticket gate asks when pressed takes a ticket the system knows before Release is offered (D8). */
  const asksTicket = to?.asks_ticket ?? null
  const [ticket, setTicket] = useState<string | null>(null)
  const mayDeploy = releasable && shipsAny && (!asksTicket || ticket !== null)
  const submit = () => void run('Could not release', async () => {
    if (!from) return
    await deploy(projectId, from.id, scheduledFor, all ? undefined : pickedChanges.map(c => c.change_id), undefined, ticket)
    setStarting(true)
    onDone()
  }, {
    mapError: (err, fallback) => {
      const detail = err instanceof ApiError && err.status === 409 ? err.detail as { code?: string; until?: string } | null : null
      if (detail?.code === 'frozen') {
        setScheduling(true)
        if (detail.until) setWhen(toLocalInput(detail.until))
      }
      return refusal(err, fallback)
    },
  })

  /* ── THE RELEASE'S OWN ACTIONS ── each the server's to offer. */
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: releaseKey(projectId) })
    void queryClient.invalidateQueries({ queryKey: ['pipeline-view', projectId] })
  }
  const act = (fallback: string, write: () => Promise<unknown>) =>
    void run(fallback, async () => { await write(); refresh() }, { mapError: refusal })
  const survey = () => void run('Could not check', async () => {
    if ((await requestSurvey(projectId))?.filed) refresh()
  }, { mapError: refusal })
  const [pending, setPending] = useState<Pending | null>(null)
  /* Approve and Reject open the approver's dialog; its refusal shows there, and it closes once the write lands. */
  const [deciding, setDeciding] = useState<ApprovalMode | null>(null)
  const decide = (releaseId: number, comment: string) => void run(
    deciding === 'reject' ? 'Could not reject' : 'Could not approve',
    async () => {
      await (deciding === 'reject' ? reject : approve)(projectId, releaseId, comment)
      setDeciding(null)
      refresh()
    },
    { mapError: refusal },
  )
  /* ── THE FREEZE OVERRIDE ── offered while a freeze holds the step to someone who may press it and edit the pipeline:
     the request it would send, with a typed reason (`OverrideFreezeDialog`). */
  const boardStep = to ? props.stepOf?.(to.id) : undefined
  const freezeName = boardStep?.can.override ? freezeHolding(boardStep) : null
  /* A step closed until its source verifies its arrival: Release is disabled, the reason on hover. */
  const closedReason = boardStep?.open === false ? boardStep.closed_reason || 'Closed' : null
  /* The freeze override's right (`can.override`); an older view that names none leaves it to the server. */
  const untestedOffered = untestedReady && boardStep?.can.override !== false
  /* Release stands at all times; when it cannot send, why is on hover. */
  const idleReason = closedReason ?? (
    from?.next_action !== 'deploy' ? `You can't release to ${to?.name.toLowerCase() ?? 'the next stage'}`
      : releasable ? null
        : untestedOffered ? 'Nothing tested yet: Release untested ships the rest'
          : 'Nothing new to release')
  /* The target's arrival: its chip on the bar, and a failure's report below it. */
  const arrival = to ? props.arrivalOf?.(to.id) ?? null : null
  const arrivalChip = <ArrivalChip arrival={arrival} />
  const arrivalReport = arrivalFailed(arrival) && arrival?.report ? (
    <Typography variant="caption" component="div" data-testid="release-arrival-report"
      sx={{ ...NOTE_SX, pl: 1, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
      {arrival.report}
    </Typography>
  ) : null
  const [overriding, setOverriding] = useState<((reason: string) => Promise<unknown>) | null>(null)
  const [releasingUntested, setReleasingUntested] = useState(false)
  const overrideButton = (testId: string, send: (reason: string) => Promise<unknown>) => (
    <Tip title={freezeName === 'Freeze now' ? 'Override Freeze now' : `Override freeze “${freezeName}”`} describeChild={false}>
      <Button size="small" color="error" disabled={busy} data-testid={testId} sx={BTN_SX}
        onClick={() => setOverriding(() => send)}>Override</Button>
    </Tip>
  )
  const overrideDialog = overriding && freezeName && to && (
    <OverrideFreezeDialog freezeName={freezeName} subject={verb} submitLabel="Override and release"
      onOverride={overriding} onClose={() => setOverriding(null)}
      onDone={() => { setOverriding(null); setStarting(true); refresh(); onDone() }} />
  )
  const last = data?.stages[data.stages.length - 1]
  const lastSource = data?.stages[data.stages.length - 2]
  /* A hotfix sends one change from the first stage straight to the last, for whoever may release there. */
  const hotfixOffered = !open && !!last && from === data?.stages[0] && lastSource?.next_action === 'deploy' && waiting.length > 0
  const runPage = planSurfaceShown(admin, 'production_qa')
  /* On every plan: Deploy tracking sits on the pipeline itself (Pipeline's Production stage, or Customize › CI pipeline). */
  const connectOffered = props.canEdit && !!to && to === last && data?.deploy_events === 'not_connected'
  const connectHref = props.customPipeline
    ? `/projects/${projectId}?tab=pipeline`
    : `/projects/${projectId}?tab=customize&section=ci-pipeline`
  const runButton = (release: Release, external: string | null, testId = 'release-run') => {
    const id = runPage ? release.deployment?.id ?? null : null
    if (id !== null) return <Button size="small" component={RouterLink} to={`/deployments/${id}`} data-testid={testId} sx={BTN_SX}>Run</Button>
    return external
      ? <Button size="small" href={external} target="_blank" rel="noopener noreferrer" data-testid={testId} sx={BTN_SX}>Run</Button>
      : null
  }
  /* Named by what it connects: the deploy events a release waits for (set up on the pipeline's Production stage). */
  const connectButton = (
    <Tip title="Track deploys, so a release finishes when its deploy does">
      <Button size="small" component={RouterLink} to={connectHref} data-testid="release-connect" sx={BTN_SX}>Connect deploys</Button>
    </Tip>
  )
  let failedState: JSX.Element | null = null
  if (failed && step) {
    const kind = failed.failure?.action ?? 'retry'
    const where = failedWhere(failed, props.runOf(step.to.id))
    const message = failed.failure?.message
    const external = kind === 'open_run' || !runPage ? safeHref(failed.deployment?.run_url) ?? safeHref(failed.failure?.url) ?? null : null
    failedState = (
      <>
        <StatusChip label={where} tone="error" glyph={<ErrorCircleIcon />} iconOnly testId="release-failed"
          title={message && message !== where ? `${where} \u2014 ${message}` : where} />
        {kind !== 'open_pr' && kind !== 'connect' && kind !== 'open_run' && data?.stages[step.toIndex - 1]?.next_action === 'deploy' && (
          <Button size="small" variant={mayDeploy ? 'outlined' : 'contained'} disabled={busy} data-testid="release-retry" sx={BTN_SX}
            onClick={() => act('Could not retry', () => retry(projectId, failed.id))}>Retry</Button>
        )}
        {freezeName && !mayDeploy && kind === 'retry' && overrideButton('release-retry-override',
          reason => retry(projectId, failed.id, reason))}
        {runButton(failed, external)}
        {kind === 'connect' && connectOffered && connectButton}
      </>
    )
  }
  const showConnect = connectOffered && !(failed && failed.failure?.action === 'connect')

  if (shown || starting) {
    const word = shown ? statusOfRelease(shown.state) : 'queued'
    const running = starting || (open !== undefined && shown === open && RUNNING.has(open.state))
    /* A queue the executor cannot start says why on hover, in the warning tone (style guide §Status prose). */
    const held = shown?.state === 'queued' ? data?.executor.wait_reason ?? null : null
    const label = shown ? inFlightLabel(shown) : STATUS.queued.label
    const tone = held ? 'warning' : word ? STATUS[word].tone : 'neutral'
    const feed = (key: string, tasks: PipelineTask[], shipping?: ReleaseParts) => (
      <ActivityFeedView key={key} tasks={tasks} {...feedProps} parts={shipping} />
    )
    return (
      <>
        <Box data-testid="release-progress-bar" data-state={shown?.state ?? 'starting'} sx={{ ...BAR_SX, pl: 1 }}>
          <Box sx={PROGRESS_STATS_SX}>
            <StatusChip label={label} tone={tone} data-tone={tone} title={held ?? undefined}
              ariaLabel={held ? `${label} — ${held}` : undefined} testId="release-progress-state" wrap />
            {to && (
              <Box component="span" sx={{ color: 'text.secondary' }} data-testid="release-progress-target">
                <span aria-hidden="true">{'\u2192 '}</span><Box component="span" sx={VISUALLY_HIDDEN}>to </Box>{to.name}
              </Box>
            )}
            {arrivalChip}
            {shown && (
              <GateResults projectId={projectId} query={{ subjectKind: 'release', subjectId: String(shown.id) }}
                title="Checks" placement="bar" testId="release-gate-results" />
            )}
          </Box>
          <Box sx={ACTIONS_SX}>
            {open?.can_approve && (
              <Button size="small" variant="contained" disabled={busy} data-testid="release-approve" sx={BTN_SX}
                onClick={() => setDeciding('approve')}>Approve</Button>
            )}
            {open?.can_reject && (
              <Button size="small" disabled={busy} data-testid="release-reject" sx={BTN_SX}
                onClick={() => setDeciding('reject')}>Reject</Button>
            )}
            {freezeName && from && open?.kind === 'promote' && (open.state === 'queued' || open.state === 'scheduled') &&
              overrideButton('release-progress-override', reason => deploy(projectId, from.id, null, undefined, reason))}
            {open?.state === 'queued' && !data?.executor.node_online && data?.can_deploy && (
              <Button size="small" component={RouterLink} to="/nodes" data-testid="release-open-nodes" sx={BTN_SX}>Open nodes</Button>
            )}
            {shown && runButton(shown, runPage ? null : safeHref(shown.deployment?.run_url) ?? null, 'release-progress-run')}
            {open?.can_cancel && (
              <Button size="small" disabled={busy} data-testid="release-progress-cancel" sx={BTN_SX}
                onClick={() => void run('Could not cancel', async () => {
                  await cancel(projectId, open.id)
                  void queryClient.invalidateQueries({ queryKey: releaseKey(projectId) })
                  onDone()
                }, { mapError: refusal })}>Cancel</Button>
            )}
          </Box>
          {running && (
            <LinearProgress data-testid="release-progress-line" aria-label="Release in progress"
              sx={{ position: 'absolute', left: 0, right: 0, bottom: 0 }} />
          )}
        </Box>
        {((formError && !deciding) || tab.isError) && (
          <Typography variant="caption" component="div" role="alert" sx={NOTE_SX}>{formError || refusal(tab.error, 'Could not load')}</Typography>
        )}
        {arrivalReport}
        {open?.state === 'awaiting_approval' && open.approvals.length > 0 && (
          <Box component="ul" data-testid="release-approvals" sx={APPROVALS_SX}>
            {open.approvals.map((a, i) => (
              <Box component="li" key={a.user?.id ?? i} data-testid="release-approval" sx={WRAP_SX}>
                {a.user?.name ?? 'Someone'}
                {a.comment && <Box component="span" sx={{ color: 'text.secondary' }}>{` \u2014 ${a.comment}`}</Box>}
              </Box>
            ))}
          </Box>
        )}
        {shown && (
          <Box sx={GATES_SX}>
            <GateResults projectId={projectId} query={{ subjectKind: 'release', subjectId: String(shown.id) }}
              title="Checks" placement="below" testId="release-gate-results" />
          </Box>
        )}
        {shown?.state === 'canceled' && shown.cancellation && cancellationWords(shown.cancellation) && (
          <Typography variant="body2" data-testid="release-cancellation" sx={{ ...WRAP_SX, pl: 1, pb: 0.5 }}>
            {cancellationWords(shown.cancellation)}
          </Typography>
        )}
        {deciding && open && (
          <ApprovalDialog mode={deciding} instructions={open.instructions} busy={busy} error={formError}
            onConfirm={comment => decide(open.id, comment)} onClose={() => setDeciding(null)} />
        )}
        <Box data-testid="release-progress" sx={props.listSx}>
          {overrideDialog}
          {shown && (
            <ReleaseFoldRow
              key={shown.id}
              projectId={projectId}
              release={{ id: shown.id, version: '', at: shown.requested_at, count: taskCount(shown.changes), open: true, features: [], run: null }}
              rows={null}
              live={running}
              filters={{}}
              runPage={false}
              renderTasks={feed}
              renderAutomatic={feed}
              shape={item => (item.task ? { ...item.task, pipeline_group: item.group, pipeline_stage: item.stage_id } : null)}
            />
          )}
        </Box>
      </>
    )
  }
  return (
    <>
      <Box data-testid="release-pick-bar" sx={pickable ? BAR_SX : BAR_BARE_SX}>
        {pickable && <Box sx={BAR_TICK_SX}>
          <Tip title="Select all" describeChild={false}>
            <Checkbox
              size="small"
              data-testid="release-pick-all"
              checked={tickedRows === rowCount}
              indeterminate={tickedRows > 0 && tickedRows < rowCount}
              disabled={busy}
              onChange={(_, on) => tickAll(on)}
              slotProps={{ input: { 'aria-label': 'Select all' } as object }}
              sx={{ ...SELECT_BOX_SX, [PHONE]: { width: SIZE.tapTarget, height: SIZE.tapTarget } }}
            />
          </Tip>
        </Box>}
        <Box sx={STATS_SX}>
          {pickable && (
            <Box component="span" sx={COUNT_SX}>
              <Stat testId="release-pick-count" tip={`${shippingShown} of ${rowCount} on ${from?.name ?? 'this stage'} ship${!anyTicks ? ': tick some to ship only those' : ''}`}>
                {`${shippingShown}/${rowCount} shipping`}
              </Stat>
            </Box>
          )}
          {hiddenShips && (
            pickable
              ? <Stat testId="release-pick-hidden" tip={`${countOf(hidden.length, 'change')} not listed here ship too`}>{`+${hidden.length}`}</Stat>
              : <Stat testId="release-pick-hidden" tip={`${countOf(hidden.length, 'change')} not listed here ship`}>{`${hidden.length} hidden`}</Stat>
          )}
          {extraShips && <Stat testId="release-pick-extra" tip={`${countOf(extra, 'commit')} with no task ship too`}>{`+${extra}`}</Stat>}
          {pin && <PinStat pin={pin} />}
          {costed.length > 0 && (
            <Stat testId="release-pick-cost" tip={`Cost ${formatCostUsd(cost)}`}>
              {`${costLabel({ cost_usd: String(cost), cost_status: 'measured' })} cost`}
            </Stat>
          )}
          {tokened.length > 0 && <Stat testId="release-pick-tokens" tip={groupedCountOf(tokens, 'token')}>{`${compactCount(tokens)} tokens`}</Stat>}
          {oldest && (
            <Stat testId="release-pick-age" tip={`Oldest merged ${shortDate(oldest, new Date(), true)}`}>{`oldest ${compactAge(oldest)}`}</Stat>
          )}
        </Box>
        <Box sx={ACTIONS_SX}>
          {arrivalChip}
          {failedState}
          {releasable && (scheduling ? (
            <TextField
              type="datetime-local"
              size="small"
              value={when}
              onChange={e => setWhen(e.target.value)}
              disabled={busy}
              autoFocus={!frozenUntil}
              sx={WHEN_SX}
              slotProps={{
                htmlInput: { 'data-testid': 'release-pick-when', 'aria-label': 'When' },
                input: {
                  startAdornment: (
                    <InputAdornment position="start" sx={{ mr: 0.5 }}>
                      <Tip title={frozenUntil ? `Frozen until ${shortDate(frozenUntil, new Date(), true)}` : 'Now'} describeChild={false}>
                        <span>
                          <IconButton size="small" edge="start" aria-label="Now" data-testid="release-pick-now"
                            disabled={busy || !!frozenUntil} onClick={() => setScheduling(false)}>
                            <ClockIcon size={SIZE.iconSm} />
                          </IconButton>
                        </span>
                      </Tip>
                    </InputAdornment>
                  ),
                },
              }}
            />
          ) : (
            <Tip title="When" describeChild={false}>
              <Button size="small" disabled={busy} data-testid="release-pick-when-now" aria-label="When: now"
                startIcon={<ClockIcon size={SIZE.iconSm} />} onClick={() => setScheduling(true)}
                sx={{ ...BTN_SX, color: 'text.secondary' }}>
                Now
              </Button>
            </Tip>
          ))}
          {(releasable || untestedOffered) && asksTicket && to && (
            <TicketField projectId={projectId} stageId={to.id} system={asksTicket} jiraProject={to.ticket_project}
              disabled={busy} onTicket={setTicket} />
          )}
          {to && (
            <Tip title={idleReason ?? verb} describeChild={false}>
              <span>
                <Button size="small" variant="contained" data-testid="release-pick-go" sx={BTN_SX}
                  disabled={!mayDeploy || busy || (scheduling && !scheduledFor) || closedReason !== null}
                  onClick={submit}>
                  {releasable && shipCount > 0 ? `Release ${shipCount}` : 'Release'}
                </Button>
              </span>
            </Tip>
          )}
          {untestedOffered && closedReason === null && (
            <Tip title={asksTicket && ticket === null ? 'Enter the ticket first'
              : `Ship all ${countOf(untestedCount, 'change')} without the full tests`} describeChild={false}>
              <Button size="small" variant={releasable ? 'text' : 'outlined'} color="warning"
                disabled={busy || (!!asksTicket && ticket === null)}
                data-testid="release-pick-untested" sx={BTN_SX} onClick={() => setReleasingUntested(true)}>
                Release untested
              </Button>
            </Tip>
          )}
          {releasable && freezeName && mayDeploy && overrideButton('release-pick-override',
            reason => deploy(projectId, from!.id, null, all ? undefined : pickedChanges.map(c => c.change_id), reason, ticket))}
          {hotfixOffered && last && (
            <Button size="small" disabled={busy} data-testid="release-hotfix" sx={BTN_SX}
              onClick={() => setPending({ kind: 'hotfix', changes: waiting, to: last })}>Hotfix</Button>
          )}
          {showConnect && connectButton}
          {data?.can_deploy && (
            <Tip title="Check what is waiting" describeChild={false}>
              <IconButton size="small" aria-label="Check what is waiting" data-testid="release-survey" disabled={busy} onClick={survey}>
                <RefreshIcon size={SIZE.iconSm} />
              </IconButton>
            </Tip>
          )}
        </Box>
      </Box>
      {overrideDialog}
      {releasingUntested && from && to && (
        <OverrideFreezeDialog untested freezeName="" subject={verb} submitLabel="Release untested"
          onOverride={reason => deploy(projectId, from.id, null, undefined, undefined, ticket, reason)}
          onClose={() => setReleasingUntested(false)}
          onDone={() => { setReleasingUntested(false); setStarting(true); refresh(); onDone() }} />
      )}
      {pending && (
        <DeployDialog projectId={projectId} pending={pending} onClose={() => setPending(null)}
          onDone={() => { setPending(null); refresh() }} />
      )}
      {failed && step && (
        <Box sx={GATES_SX}>
          <GateResults projectId={projectId} query={{ subjectKind: 'release', subjectId: String(failed.id) }}
            title={`Checks before ${step.to.name}`} testId="release-gate-results" />
        </Box>
      )}
      {from && (
        <Box sx={GATES_SX}>
          <GateResults projectId={projectId} query={{ subjectKind: 'arrival', stageId: from.id }}
            title={`Checks on ${from.name}`} testId="release-arrival-gate-results" />
        </Box>
      )}
      {arrivalReport}
      {(formError || tab.isError || items.isError) && (
        <Typography variant="caption" component="div" role="alert" data-testid="release-pick-error" sx={NOTE_SX}>
          {formError || refusal(tab.error ?? items.error, 'Could not load')}
        </Typography>
      )}
      <Box data-testid="release-pick" sx={props.listSx}>
        <WaitNote reasons={props.reasons} />
        {(tasks.length > 0 || items.isPending) && (
          <ActivityFeedView
            tasks={tasks}
            loading={items.isPending || tab.isPending}
            {...feedProps}
            selection={selection}
            parts={parts}
          />
        )}
        {!items.isPending && plain.map(change => (
          <Box key={change.change_id} data-testid="release-pick-change" sx={CHANGE_ROW_SX}>
            <Box sx={RAIL_CENTRED_SX}>
              <Checkbox
                size="small"
                checked={plainOn.has(change.change_id)}
                disabled={busy}
                onChange={() => togglePlain(change.change_id)}
                slotProps={{ input: { 'aria-label': change.title } as object }}
                sx={SELECT_BOX_SX}
              />
            </Box>
            <Box component="span" sx={{ flex: '1 1 auto', minWidth: 0, overflowWrap: 'anywhere' }}>{change.title}</Box>
            {change.merged_at && <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>{compactAge(change.merged_at)}</Box>}
          </Box>
        ))}
        {laterShown > 0 && lanePin?.sha && (
          <Box data-testid="release-untested">
            <Tip title={`Merged after ${lanePin.sha.slice(0, 7)}, the newest tested commit: they ship in a later release, once tested`} describeChild={false}>
              <Box component="h3" tabIndex={0} data-testid="release-untested-heading" sx={UNTESTED_HEADING_SX}>
                {`Waiting for tests \u00b7 ${laterShown}`}
              </Box>
            </Tip>
            {lanePin.stall && (
              <Box data-testid="release-untested-stall" sx={STALL_SX}>
                {stallLine(lanePin.stall)}
                {lanePin.stall.check_task_id !== null && (
                  <> · <Link component={RouterLink} to={`/tasks/${lanePin.stall.check_task_id}`}>View check</Link></>
                )}
              </Box>
            )}
            {later.tasks.length > 0 && (
              <ActivityFeedView
                tasks={later.tasks}
                {...feedProps}
                parts={laterParts}
              />
            )}
            {!items.isPending && later.plain.map(change => (
              <Box key={change.change_id} data-testid="release-untested-change" sx={UNTESTED_ROW_SX}>
                <Box component="span" sx={{ flex: '1 1 auto', minWidth: 0, overflowWrap: 'anywhere' }}>{change.title}</Box>
                {change.merged_at && <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>{compactAge(change.merged_at)}</Box>}
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </>
  )
}
