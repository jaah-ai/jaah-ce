/* THE PIPELINE PAGE'S LIST — one section per stage (AC-8/11/12): its name, its count, a tick-all, and its rows in
 * the board's own feed (`ActivityFeedView`: every row verb, epics with children, Group inside the section). The rows
 * are the stage's items (`GET …/pipeline/items`), and the view is read under the same filters, so a section holds
 * exactly what its count says.
 * TICK-ALL PICKS THE WHOLE STAGE, not the rows on screen: the verb then goes as `all_in_stage` with the list's
 * filters, and the server resolves every item (AC-11). The dock offers the stage's own verbs: its step's forward
 * verb (Start, Merge, Move), Approve where that step needs approvals, Archive, Delete drafts, and Restore under
 * Archived. A redraw that drops picks says so, as the board's does. No drag and drop anywhere.
 * A STAGE'S ROWS ARE ONE FLAT LIST, no group headings: questions, then failures, then stops, then the rest newest
 * first (`pipeline/membership.py::_GROUP_RANK`). A merging row is listed only under its merge step's count.
 * ROWS LOAD A PAGE AT A TIME (`PAGE_LIMIT`), the picked stage's alone, and the next page when the list's end is
 * reached (`useLoadMoreSentinel`): a stage opens on one small read however long its history.
 * A RELEASE-TARGET STAGE (Production) LISTS WHAT NEEDS SOMEONE FIRST — its rows asking, failed or failed to merge
 * (`ATTENTION_GROUPS`), questions first, with every row verb (Start retries, Complete settles it) —
 * then its releases, which leave those rows out.
 * THE FIRST STAGE (Ideas) ENDS IN A CLOSED "Done N" ROW: the finished tasks that shipped no code, which no stage holds
 * (`DoneFold.tsx`), so a done task never vanishes from the board.
 * ONE STAGE PICKED DRAWS NO HEADING — its block above already names and counts it — and its tick-all sits at the
 * head of the create row. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { replaceEqualDeep, useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import Typography from '@mui/material/Typography'
import Tip from '../../ui/Tip'
import { apiDetail } from '../../api'
import ActivityFeedView, { FeedNewTaskRow } from '../tasks/listViews/ActivityFeedView'
import type { PaneSelection, TaskListFold, TaskListViewProps } from '../tasks/listViews/types'
import ReleaseList, { CHANGE_ROW_SX } from './ReleaseList'
import DoneFold from './DoneFold'
import ApprovalDialog, { type ApprovalMode } from './ApprovalDialog'
import OverrideFreezeDialog from './OverrideFreezeDialog'
import BulkActionBar, { type BulkAction } from '../../ui/BulkActionBar'
import Banner from '../../ui/Banner'
import { clearPicks, createPickStore, togglePick, usePicks } from '../../lib/pickStore'
import { useLoadMoreSentinel } from '../../hooks/useLoadMoreSentinel'
import { usePhone } from '../../hooks/usePhone'
import { compactAge } from '../../lib/relativeTime'
import { countOf } from '../../lib/humanize'
import { GROUP_BY_NONE, GROUP_BY_STATE } from '../../lib/taskGrouping'
import { PHONE, SELECT_BOX_SX } from '../../ui/listStyles'
import { rowPadLeft } from '../../ui/feed/geometry'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, SIZE, grid } from '../../theme'
import { type ConfirmOptions, danger } from '../ConfirmDialog'
import type { PipelineTask } from '../../types'
import { WaitNote, stepFrom } from './WaitChip'
import { FORM_ACTION_BAR_ATTR } from '../../ui/FormActionBar'
import { useLauncherYield } from '../../ui/launcherSeat'
import {
  ATTENTION_GROUPS,
  BUCKETS,
  ON_STEP_ONLY,
  SUBJECTS_MAX,
  actOnStep,
  bulkItems,
  filtersBody,
  forwardVerb,
  freezeHolding,
  getPipelineItemsPage,
  refreshPipelineItems,
  isBucket,
  outcomeWords,
  pipelineItemsKey,
  type ActOutcome,
  type ItemFilters,
  type PipelineItem,
  type PipelineView,
  type ReleaseParts,
  type Subjects,
} from '../../lib/pipelineBoard'

const SECTION_HEAD_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  minHeight: grid(5),
  mt: 1.5,
  borderBottom: '1px solid',
  borderColor: 'divider',
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const
const SECTION_NAME_SX = { fontSize: FONT_SIZE.base, fontWeight: FONT_WEIGHT.semibold, lineHeight: LINE_HEIGHT.snug } as const
const SECTION_COUNT_SX = { fontSize: FONT_SIZE.sm, color: 'text.secondary' } as const
const NOTE_SX = { color: 'text.secondary' } as const

type Section = { key: string; stageId: string | null; label: string; tasks: PipelineTask[]; changes: PipelineItem[] }

export type PipelineSectionsProps = Pick<
  TaskListViewProps,
  'grouping' | 'onToggleSection' | 'tree' | 'actions' | 'submitting' | 'expanded' | 'fetching' | 'focus' |
  'onToggleTask' | 'deepLinkedId' | 'hiddenColumns' | 'create'
> & {
  projectId: number
  view: PipelineView | undefined
  /** `?stage=` (a stage id or a bucket) and `?only=` (an in-stage group). */
  stage: string | null
  only: string | null
  filters: ItemFilters
  /** The board's Group by; State becomes the stage's own groups. */
  groupBy: string | undefined
  canEdit: boolean
  canDelete: boolean
  /** False while the page cannot yet say which org a step verb goes as (`Tasks.tsx`): the verbs wait. */
  stepVerbs?: boolean
  confirm: (options: ConfirmOptions) => Promise<boolean>
  /** After a verb lands: the board's own re-read. */
  onChanged: () => void
  filtered: boolean
}

export default function PipelineSections(props: PipelineSectionsProps) {
  const { projectId, view, stage, only, filters, canEdit, canDelete, stepVerbs = true, confirm, onChanged } = props
  const queryClient = useQueryClient()
  const bucket = isBucket(stage) ? stage : null
  const stageIds = useMemo(() => new Set(view?.stages.map(s => s.id) ?? []), [view])
  const pickedStage = stage !== null && stageIds.has(stage) ? stage : null
  const inFlight = !!view?.steps.some(s => s.in_flight > 0)
  const filtersKey = JSON.stringify(filters)
  const itemsPick = { stage: pickedStage ?? bucket, group: only }
  const itemsQuery = useInfiniteQuery({
    queryKey: [...pipelineItemsKey(projectId), itemsPick.stage, itemsPick.group, filtersKey],
    queryFn: ({ signal, pageParam }) => getPipelineItemsPage(projectId, itemsPick, filters, pageParam, signal),
    initialPageParam: null as string | null,
    // A picked stage is known only once the view names the stages: no read of every stage in the meantime.
    enabled: stage === null || bucket !== null || view !== undefined,
    getNextPageParam: last => last.next ?? undefined,
    // The last answer stays up while the next loads — this project's only, never another's rows under its name.
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === projectId ? prev : undefined),
    meta: { persist: false },
  })

  /* A row keeps its object while unchanged, so the feed's memoized rows skip their render on a refetch. */
  const heldRef = useRef(new Map<number, PipelineTask>())
  /* A release's rows (`ReleaseList`), shaped as the section's: the held row when listed, else the item's own. */
  const shapeItem = (item: PipelineItem): PipelineTask | null => item.task
    ? heldRef.current.get(item.task.id) ?? { ...item.task, pipeline_group: item.group, pipeline_stage: item.stage_id }
    : null
  /* Stages a release arrives on: their section is the releases, last open (AC-10); a filter keeps the releases that
     carried a listed item. */
  const releaseTargets = useMemo(() => new Set(
    (view?.steps ?? []).filter(s => s.action === 'release').map(s => s.to_stage_id)), [view])
  const pages = itemsQuery.data?.pages
  const items = useMemo(() => pages?.flatMap(page => page.rows), [pages])
  const shaped = useMemo(() => {
    const held = heldRef.current
    const next = new Map<number, PipelineTask>()
    const out: { item: PipelineItem; task: PipelineTask | null }[] = []
    for (const item of items ?? []) {
      if (!item.task) {
        out.push({ item, task: null })
        continue
      }
      const fresh: PipelineTask = { ...item.task, pipeline_group: item.group, pipeline_stage: item.stage_id }
      const old = held.get(fresh.id)
      const task = old ? replaceEqualDeep(old, fresh) : fresh
      next.set(task.id, task)
      out.push({ item, task })
    }
    heldRef.current = next
    return out
  }, [items])

  /* Hidden tasks listed for the change they merged (`board.py::_automatic_tasks`): each stage folds them. */
  const automaticIds = useMemo(
    () => new Set((items ?? []).flatMap(item => (item.automatic && item.task ? [item.task.id] : []))), [items])
  /* The folds the viewer flipped from their default: closed, except the first stage's (Ideas by default), which opens
     there, since nothing else in that stage hides it. */
  const [automaticFlipped, setAutomaticFlipped] = useState<ReadonlySet<string>>(() => new Set())
  const openByDefault = view?.stages[0]?.id
  const automaticOpen = { has: (key: string) => automaticFlipped.has(key) !== (key === openByDefault) }
  /* One fold object per stage while its state holds, so the fold's feed keeps its memoized sections. */
  const folds = useRef(new Map<string, TaskListFold>())
  /** `stage`: the fold of a stage's own rows, whose open state its tick-all reads (`pickableIn`); a release's fold is
   *  not, so flipping it keeps a whole-stage tick. */
  const foldOf = (key: string, count: number, stage = true): TaskListFold => {
    const open = automaticOpen.has(key)
    const held = folds.current.get(key)
    if (held && held.open === open && held.count === count) return held
    const fold = {
      label: 'Automatic', open, count,
      // Opening or closing a stage's fold ends its whole-stage tick: the picks stay, as the rows they are.
      onToggle: () => (stage && setAllIn(null), setAutomaticFlipped(prev => {
        const next = new Set(prev)
        if (!next.delete(key)) next.add(key)
        return next
      })),
    }
    folds.current.set(key, fold)
    return fold
  }
  /** A stage's picks: its folded Automatic tasks only while the fold is open (a pick is what the viewer sees). */
  const pickableIn = (section: Section) => section.tasks.filter(t => t.parent_task_id === null &&
    (automaticOpen.has(section.key) || !automaticIds.has(t.id)))

  const sections: Section[] = useMemo(() => {
    const rows = shaped.filter(({ item }) => only === null || item.group === only)
    const sectionOf = (key: string, stageId: string | null, label: string, members: typeof rows): Section => ({
      key, stageId, label,
      tasks: members.flatMap(({ task }) => (task ? [task] : [])),
      changes: members.flatMap(({ item, task }) => (task ? [] : [item])),
    })
    if (bucket) return [sectionOf(bucket, null, BUCKETS.find(([id]) => id === bucket)![1], rows)]
    const stages = (view?.stages ?? []).filter(s => pickedStage === null || s.id === pickedStage)
    // A step's own group (Merging) spans every stage, as its count does: the picked section draws every row read.
    const spans = pickedStage !== null && only !== null && ON_STEP_ONLY.has(only)
    return stages.map(s => sectionOf(s.id, s.id, s.name, rows.filter(({ item }) => spans || item.stage_id === s.id)))
  }, [shaped, only, bucket, view, pickedStage])

  /* One stage or bucket picked: the only section, drawn without its heading. */
  const lone = sections.length === 1 && (pickedStage !== null || bucket !== null) ? sections[0] : null

  /* ── THE PICK ── */
  const [store] = useState(() => createPickStore<PipelineTask>())
  const picks = usePicks(store)
  /* While a step is in flight the list re-reads itself, cut back to its first pages (`refreshPipelineItems`) — whole
     while rows are picked. */
  useEffect(() => {
    if (!inFlight) return
    const timer = setInterval(() => refreshPipelineItems(queryClient, projectId, store.get().size > 0), 10_000)
    return () => clearInterval(timer)
  }, [inFlight, queryClient, projectId, store])
  /** A whole stage (or bucket) ticked: its verbs go `all_in_stage`. */
  const [allIn, setAllIn] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  /** The approver's dialog, open for Approve or Reject. */
  const [deciding, setDeciding] = useState<ApprovalMode | null>(null)
  useEffect(() => {
    if (note === null) return
    const timer = setTimeout(() => setNote(null), 6_000)
    return () => clearTimeout(timer)
  }, [note])
  /* A redraw keeps the picks still listed and says what it dropped. */
  useEffect(() => {
    if (running) return
    const listed = new Map(sections.flatMap(s => s.tasks).map(task => [task.id, task]))
    const prev = store.get()
    if (prev.size === 0) return
    const next = new Map<number, PipelineTask>()
    for (const id of prev.keys()) {
      const task = listed.get(id)
      if (task) next.set(id, task)
    }
    if (next.size === prev.size && [...next].every(([id, task]) => prev.get(id) === task)) return
    store.update(() => next)
    if (next.size < prev.size) setNote(next.size === 0 ? 'Selection cleared' : `Kept ${next.size} of ${countOf(prev.size, 'pick')}`)
    if (allIn !== null && !sections.some(s => s.key === allIn)) setAllIn(null)
  }, [sections])

  const clear = () => {
    setAllIn(null)
    clearPicks(store)
  }
  const tickAll = (section: Section, on: boolean) => {
    setNote(null)
    if (!on) return clear()
    setAllIn(section.key)
    store.update(() => new Map(pickableIn(section).map(t => [t.id, t])))
  }
  const selections = useRef(new Map<string, PaneSelection>())
  const selectionFor = (key: string): PaneSelection => {
    let held = selections.current.get(key)
    if (!held) {
      held = {
        store,
        isSelectable: task => task.parent_task_id === null,
        onToggle: task => {
          setNote(null)
          setAllIn(null)
          togglePick(store, task.id, task)
        },
        onDrawn: () => {},
      }
      selections.current.set(key, held)
    }
    return held
  }

  /* ── THE VERBS ── */
  const allSection = allIn !== null ? sections.find(s => s.key === allIn) : undefined
  const pickedTasks = [...picks.values()]
  const pickStages = new Set(pickedTasks.map(t => t.pipeline_stage ?? null))
  const verbStage = allSection ? allSection.stageId : pickStages.size === 1 ? [...pickStages][0] : null
  const stageIndex = verbStage && view ? view.stages.findIndex(s => s.id === verbStage) : -1
  const next = stageIndex >= 0 ? view?.stages[stageIndex + 1] : undefined
  const step = next ? view?.steps.find(s => s.to_stage_id === next.id) : undefined
  const forward = stepVerbs && step?.can.act ? forwardVerb(step, next) : null
  /* THE SERVER RESOLVES A WHOLE STAGE whenever the list is what the server would pick — a step count's group goes
     with it, and a closed Automatic fold stays out of it (`folded: false`). The view is read under the list's
     filters, so a stage's count is its listed rows'. */
  const stageTotal = allSection?.stageId
    ? view?.stages.find(s => s.id === allSection.stageId)?.count
    : allSection && bucket ? view?.[bucket] : undefined
  /* A whole stage's count is the server's: the stage's own, or — a step count's group — what this read's pick holds. */
  const readTotal = lone !== null && allSection === lone && !itemsQuery.isPlaceholderData ? pages?.[0]?.total : undefined
  const serverTotal = only === null ? stageTotal : readTotal
  /* …and only with that count in hand. */
  const wholeStage = allSection !== undefined && serverTotal !== undefined
  const foldOpen = allSection !== undefined && automaticOpen.has(allSection.key)
  /* What the pick leaves folded, by the server's own count (the first page's, whatever is loaded). */
  const foldedOut = allSection && !foldOpen && !itemsQuery.isPlaceholderData ? pages?.[0]?.folded[allSection.key] ?? 0 : 0
  const named = (allSection ? pickableIn(allSection) : pickedTasks).map(t => t.id)
  const count = !allSection ? picks.size : wholeStage ? serverTotal - foldedOut : named.length
  const subjects = (): Subjects => ({
    ...(wholeStage
      ? {
          all_in_stage: true, stage: allSection!.key, ...(only ? { group: only } : {}),
          ...(foldOpen ? {} : { folded: false }), filters: filtersBody(filters),
        }
      : { task_ids: named }),
    include_hidden: !!filters.showHidden,
  })
  /* The server names at most `SUBJECTS_MAX` items; a larger pick is refused here, with its reason, not there. */
  const tooMany = !wholeStage && named.length > SUBJECTS_MAX
  const blocked = running || tooMany
  const blockedReason = tooMany ? `At most ${SUBJECTS_MAX} at once` : undefined
  const drafts = (allSection ? pickableIn(allSection) : pickedTasks).filter(t => t.state === 'draft' && t.parent_task_id === null)

  async function run(done: string, call: () => Promise<ActOutcome>) {
    setRunning(true)
    setError('')
    try {
      const outcome = await call()
      setNote(outcomeWords(done, outcome))
      clear()
    } catch (err) {
      setError(apiDetail(err, `${done} failed`))
    } finally {
      setRunning(false)
      refreshAll()
    }
  }
  function refreshAll() {
    void queryClient.invalidateQueries({ queryKey: ['pipeline-view', projectId] })
    refreshPipelineItems(queryClient, projectId)
    onChanged()
  }
  /* A Merge a freeze on the integration stage holds: its press with a typed reason (R7.5, `OverrideFreezeDialog`). */
  const [overriding, setOverriding] = useState(false)
  const mergeFreeze = step && (step.action === 'merge' || step.action === 'start_merge') && step.can.override
    ? freezeHolding(step) : null

  const actions: BulkAction[] = []
  /* A step whose source stage has not verified its arrival is closed (absent on an older server: open). */
  const closedReason = step?.open === false ? step.closed_reason || 'Closed' : undefined
  if (step && next && forward && bucket === null) {
    actions.push({
      id: 'go', label: `${forward} ${count}`, disabled: blocked || closedReason !== undefined,
      disabledReason: blockedReason ?? closedReason,
      onRun: () => void run(forward === 'Start' ? 'Started' : forward === 'Merge' ? 'Merging' : 'Moved',
        () => actOnStep(projectId, next.id, 'go', subjects())),
    })
  }
  if (step && next && forward === 'Merge' && mergeFreeze && bucket === null) {
    actions.push({
      id: 'override', label: 'Override freeze', destructive: true, disabled: blocked, disabledReason: blockedReason,
      onRun: () => setOverriding(true),
    })
  }
  /* A release step's Approve is the release's own (on its arrow), never a pick's. Approve and Reject open the
     approver's dialog: the step's instructions, an optional comment. */
  const approvalStep = stepVerbs && step && next && step.action !== 'release' && step.approvals.need > 0 && bucket === null
  if (approvalStep && step.can.approve) {
    actions.push({
      id: 'approve', label: `Approve ${count}`, disabled: blocked, disabledReason: blockedReason,
      onRun: () => setDeciding('approve'),
    })
  }
  if (approvalStep && step.can.reject) {
    actions.push({
      id: 'reject', label: `Reject ${count}`, disabled: blocked, disabledReason: blockedReason,
      onRun: () => setDeciding('reject'),
    })
  }
  const instructions = step?.reasons?.find(r => r.kind === 'instructions')
  const decide = (comment: string) => {
    const verb = deciding
    if (!verb || !next) return
    setDeciding(null)
    void run(verb === 'approve' ? 'Approved' : 'Rejected', () => actOnStep(projectId, next.id, verb, subjects(), { comment }))
  }
  if (canEdit && bucket !== 'archived') {
    actions.push({
      id: 'archive', label: `Archive ${count}`, disabled: blocked, disabledReason: blockedReason,
      onRun: () => void run('Archived', () => bulkItems(projectId, 'archive', subjects())),
    })
  }
  if (canEdit && bucket === 'archived') {
    actions.push({
      id: 'restore', label: `Restore ${count}`, disabled: blocked, disabledReason: blockedReason,
      onRun: () => void run('Restored', () => bulkItems(projectId, 'restore', subjects())),
    })
  }
  if (canDelete && drafts.length > 0) {
    actions.push({
      id: 'delete', label: `Delete ${countOf(drafts.length, 'draft')}`, destructive: true,
      disabled: blocked || drafts.length > SUBJECTS_MAX,
      disabledReason: drafts.length > SUBJECTS_MAX ? `At most ${SUBJECTS_MAX} at once` : blockedReason,
      onRun: () => void (async () => {
        const ok = await confirm(danger('Delete', {
          title: `Delete ${countOf(drafts.length, 'draft')}?`,
          message: 'Each draft and its attachments are deleted for good — this cannot be undone.',
        }))
        // A delete names the drafts it counted: never more than the confirm said, whatever is not loaded yet.
        if (ok) await run('Deleted', () => bulkItems(projectId, 'delete_drafts', {
          task_ids: drafts.map(t => t.id), include_hidden: !!filters.showHidden,
        }))
      })(),
    })
  }

  /* ── THE NEXT PAGE ── read when the list's end is reached; a failed read waits for the next change. */
  const canReadMore = itemsQuery.hasNextPage && !itemsQuery.isFetchingNextPage && !itemsQuery.isError &&
    !itemsQuery.isPlaceholderData
  const sentinelRef = useLoadMoreSentinel(canReadMore, () => itemsQuery.fetchNextPage())

  /* A stage is one flat list in the server's order (questions, failures, stops, then the rest): State groups nothing. */
  const innerGroupBy = props.groupBy === GROUP_BY_STATE ? GROUP_BY_NONE : props.groupBy
  const loading = itemsQuery.isPending
  const dockShown = picks.size > 0 || allIn !== null || note !== null || error !== ''
  const dockRef = useRef<HTMLDivElement>(null)
  /* The dock is one line on a phone: its verbs carry a stage name and a count ("Move to Production 12"), so one stays
     inline (its words ellipsized if they still do not fit) and the ⋯ folds the rest. Wider, `BulkActionBar`'s three. */
  const maxInline = usePhone() ? 1 : undefined
  useLauncherYield(dockRef, dockShown)

  /* What every feed of the section is drawn with, whichever shape it takes. */
  const feedProps = (tasks: PipelineTask[], selectionKey: string) => ({
    tasks,
    deepLinkedId: props.deepLinkedId,
    tree: props.tree,
    actions: props.actions,
    submitting: props.submitting,
    expanded: props.expanded,
    fetching: props.fetching,
    focus: props.focus,
    filtered: props.filtered,
    onToggleTask: props.onToggleTask,
    selection: canEdit || canDelete ? selectionFor(selectionKey) : undefined,
    hiddenColumns: props.hiddenColumns,
  })
  const feed = (key: string, tasks: PipelineTask[], busy: boolean, selectionKey: string, groupBy = innerGroupBy,
    parts?: ReleaseParts) => (
    <ActivityFeedView key={key} {...feedProps(tasks, selectionKey)} loading={busy} groupBy={groupBy}
      grouping={props.grouping} onToggleSection={props.onToggleSection} parts={parts} />
  )
  /* An Automatic fold: one epic-style row (`ActivityFeedView`'s `foldRow`), its tasks under it when open. */
  const automaticFeed = (key: string, tasks: PipelineTask[], fold: TaskListFold, selectionKey: string) => (
    <ActivityFeedView key={key} {...feedProps(tasks, selectionKey)} fold={fold} />
  )
  /* A stage's own rows, then its Automatic fold (closed until opened): hidden tasks drawn as every other row, and under
     them the rare change whose task is gone or out of the viewer's sight, as a plain line. */
  const plain = (section: Section, shown: PipelineTask[]) => {
    const own = shown.filter(task => !automaticIds.has(task.id))
    const automatic = shown.filter(task => automaticIds.has(task.id))
    const folded = section.tasks.reduce((n, task) => n + (automaticIds.has(task.id) ? 1 : 0), 0) + section.changes.length
    const fold = folded > 0 ? foldOf(section.key, folded) : null
    return (
      <>
        {(own.length > 0 || loading) && feed(section.key, own, loading, section.key)}
        {fold && automaticFeed(`${section.key}:automatic`, automatic, fold, section.key)}
        {fold?.open && section.changes.map(({ key, change }) => change && (
          <Box key={key} data-testid="pipeline-change" sx={[CHANGE_ROW_SX, rowPadLeft(1)]}>
            <Box component="span" sx={{ flex: '1 1 auto', minWidth: 0, overflowWrap: 'anywhere' }}>{change.title}</Box>
            {change.merged_at && <Box component="span" sx={{ ...NOTE_SX, fontSize: FONT_SIZE.sm }}>{compactAge(change.merged_at)}</Box>}
          </Box>
        ))}
      </>
    )
  }

  /** A release-target stage's rows that need someone (`ATTENTION_GROUPS`): listed above its releases, never twice. */
  const attention = useMemo(() => new Map(sections.map(section => [section.key, new Set(section.tasks
    .filter(t => t.parent_task_id === null && ATTENTION_GROUPS.has(t.pipeline_group ?? '')).map(t => t.id))])), [sections])
  const attentionOf = (section: Section) => attention.get(section.key) ?? new Set<number>()
  const attentionFeed = (section: Section) => {
    const ids = attentionOf(section)
    const tasks = section.tasks.filter(t => ids.has(t.id))
    return tasks.length > 0
      ? <Box data-testid="pipeline-attention">{feed(`${section.key}:attention`, tasks, false, section.key, GROUP_BY_NONE)}</Box>
      : null
  }

  /** A section's tick-all: every item of its stage, loaded or not. */
  const tick = (section: Section) => (
    <Tip title={`Select all in ${section.label}`} describeChild={false}>
      <Checkbox
        size="small"
        data-testid="section-tick-all"
        checked={allIn === section.key}
        disabled={pickableIn(section).length === 0 || running || itemsQuery.isPlaceholderData}
        onChange={(_, on) => tickAll(section, on)}
        slotProps={{ input: { 'aria-label': `Select all in ${section.label}` } as object }}
        sx={{ ...(section === lone ? SELECT_BOX_SX : {}), [PHONE]: { width: SIZE.tapTarget, height: SIZE.tapTarget } }}
      />
    </Tip>
  )

  return (
    <Box data-testid="pipeline-sections">
      {(props.create || lone) && (
        <FeedNewTaskRow create={props.create} leading={lone && (canEdit || canDelete) ? tick(lone) : undefined} />
      )}
      {only === 'waiting' && <WaitNote reasons={stepFrom(view, stage)?.reasons} />}
      {itemsQuery.isError && <Banner severity="error">{apiDetail(itemsQuery.error, 'Could not load the pipeline')}</Banner>}
      {sections.map(section => {
        const shown = section.tasks
        const sectionCount = section.stageId
          ? (only === null ? view?.stages.find(s => s.id === section.stageId)?.count : undefined)
          : (only === null && bucket ? view?.[bucket] : undefined)
        return (
          <Box key={section.key} component="section" aria-label={section.label} data-testid="pipeline-section" data-stage={section.label}>
            {section !== lone && (
              <Box sx={SECTION_HEAD_SX}>
                {(canEdit || canDelete) && tick(section)}
                <Box component="h2" sx={{ ...SECTION_NAME_SX, m: 0 }}>{section.label}</Box>
                <Box component="span" data-testid="section-count" sx={SECTION_COUNT_SX}>
                  {sectionCount ?? section.tasks.length + section.changes.length}
                </Box>
              </Box>
            )}
            {section.stageId !== null && releaseTargets.has(section.stageId) && only === null ? (
              <>
              {attentionFeed(section)}
              <ReleaseList
                projectId={projectId}
                stageId={section.stageId}
                filters={filters}
                withRuns={section.stageId === view?.stages[view.stages.length - 1]?.id}
                shape={shapeItem}
                renderTasks={(key, tasks, parts) => {
                  const rest = tasks.filter(t => !attentionOf(section).has(t.id))
                  return rest.length > 0 ? feed(key, rest, false, section.key, GROUP_BY_NONE, parts) : null
                }}
                renderAutomatic={(key, tasks) => automaticFeed(key, tasks, foldOf(key, tasks.length, false), section.key)}
                fallback={plain(section, shown.filter(t => !attentionOf(section).has(t.id)))}
              />
              </>
            ) : plain(section, shown)}
            {section.stageId !== null && section.stageId === view?.stages[0]?.id && only === null && (
              <DoneFold projectId={projectId} filters={filters} count={view.done}
                render={(tasks, fold, busy) => (
                  // No picks: a done task has no stage, so it would leave a stage's verbs without a stage to act on.
                  <ActivityFeedView key={`${section.key}:done`} {...feedProps(tasks, section.key)} selection={undefined}
                    fold={fold} loading={busy} />
                )} />
            )}
          </Box>
        )
      })}
      {itemsQuery.hasNextPage && <Box ref={sentinelRef} aria-hidden="true" role="presentation" sx={{ height: 1 }} />}
      {dockShown && (
        <Box ref={dockRef} data-testid="pipeline-bulk-dock" {...{ [FORM_ACTION_BAR_ATTR]: '' }} sx={{ position: 'sticky', bottom: 0, zIndex: 1, bgcolor: 'background.default', py: 1, display: 'flex', flexDirection: 'column', gap: 1 }}>
          {note !== null && <Typography variant="caption" component="div" role="status" data-testid="pipeline-pick-note" sx={NOTE_SX}>{note}</Typography>}
          {error && <Typography variant="caption" component="div" role="alert" sx={{ color: 'error.main' }}>{error}</Typography>}
          <BulkActionBar count={picks.size > 0 || allIn !== null ? (count ?? picks.size) : 0} onClear={clear} actions={actions} maxInline={maxInline} label="Bulk task actions" />
        </Box>
      )}
      {deciding && (
        <ApprovalDialog mode={deciding} instructions={instructions?.kind === 'instructions' ? instructions.text : ''}
          onConfirm={decide} onClose={() => setDeciding(null)} />
      )}
      {overriding && mergeFreeze && next && (
        <OverrideFreezeDialog freezeName={mergeFreeze} subject={`Merge ${countOf(count ?? picks.size, 'task')}`}
          submitLabel="Override and merge" onClose={() => setOverriding(false)}
          onOverride={async reason => {
            const outcome = await actOnStep(projectId, next.id, 'go', subjects(), { overrideReason: reason })
            /* Nothing pressed (every item refused): the dialog says so and stays open. */
            if (outcome.done === 0) throw new Error(outcomeWords('Merging', outcome))
            setNote(outcomeWords('Merging', outcome))
          }}
          onDone={() => { setOverriding(false); clear(); refreshAll() }} />
      )}
    </Box>
  )
}
