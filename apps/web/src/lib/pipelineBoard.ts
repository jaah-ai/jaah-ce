import type { InfiniteData, QueryClient } from '@tanstack/react-query'
import { get, getPage, post } from '../api'
import type { PipelineTask } from '../types'
import type { StageHealth, StepAction, TargetKind } from './pipeline'

/* THE DAILY PIPELINE PAGE'S READS AND WRITES — the web half of `apps/api/app/routes/pipeline_board.py` (rules:
 * `apps/api/app/pipeline/`): the strip (`view`), a project's items (`items`), a step's verb on many items (`act`)
 * and archive/restore/delete drafts (`bulk`). Setup is `lib/pipeline.ts`. */

export type StageLive = { version: string; since: string | null }
/** Where the stage's newest arrival stands: its deploy, then its verify gates (`passed` of `total`), and whether it may
 *  be rolled back (to `rollback_to_version`). `report`: what a failed deploy or verify said, plain text. */
export type ArrivalState =
  | 'deploying' | 'arrived' | 'deploy_failed' | 'verifying' | 'verified' | 'verify_failed' | 'superseded' | 'rolled_back'
export type StageArrival = {
  state: ArrivalState
  version: string | null
  sha: string
  passed: number
  total: number
  report: string | null
  rollback_allowed: boolean
  rollback_to_version: string | null
}
/** `asking`: the stage's tasks waiting for an answer (the list's Needs your answer group); `stopped`: its Stopped group. */
export type ViewStage = {
  id: string; name: string; count: number; failed: number; stopped: number; asking: number; live: StageLive | null
  /** The health dot: null without an address. Absent from an older server. */
  health?: StageHealth | null
  /** Where the address comes from on a stage that deploys; null on one that never has an address. */
  target?: TargetKind | null
  /** The newest arrival's deploy and verify; null before any. Absent from an older server. */
  arrival?: StageArrival | null
}
/** A release's deploy run (`/deployments/:id`): its status and, when it failed, where. */
export type ReleaseRun = { id: number; status: string; failing_job: string | null; failing_step: string | null }
export type ReleaseBrief = {
  id: number
  state: string
  version: string
  scheduled_for: string | null
  failure: { message?: string } | null
  run_task_id: number | null
  run: ReleaseRun | null
}
/** A release that arrived on a release-target stage (`board.py::releases_of`), newest first; only the newest carries
 *  its features, the rest load on unfold. A group's `name` is null for the items outside any feature. */
export type ReleaseFold = {
  id: number
  version: string
  at: string | null
  count: number
  open: boolean
  features: { name: string | null; items: string[] }[]
  run: ReleaseRun | null
  /** The open release's epics listed for their children's merges: `{epic id: the children it ships}`. */
  parts?: Record<string, number[]>
}
export type ViewStep = {
  to_stage_id: string
  action: StepAction
  in_flight: number
  failed: number
  waiting: number
  approvals: { have: number; need: number }
  release: ReleaseBrief | null
  can: {
    act: boolean; approve: boolean; reject: boolean; cancel: boolean; retry: boolean; schedule: boolean; hotfix: boolean
    /** Run a press through a freeze (may press the step and edit the pipeline); Resume a paused by-itself step. */
    override?: boolean; resume?: boolean
  }
  freeze: string | null
  /** Why the step waits, in display order: the first blocking one is its chip (`app/pipeline/reasons.py`). */
  reasons?: WaitReason[]
  /** False while the source stage's arrival is not yet verified: the press is refused, `closed_reason` says why.
   *  Absent from an older server (open). */
  open?: boolean
  closed_reason?: string | null
}

/* WHY A STEP WAITS — `app/pipeline/reasons.py`'s kinds, pinned (architecture §5.3). Times are ISO UTC; `hours`
 * names its own zone. `instructions`, `timeout` and `freeze_soon` never hold a step. */
export type Named = { id: string; name: string }
export type WaitReason =
  | { kind: 'freeze_now'; by_name: string | null; at: string }
  | { kind: 'freeze'; name: string; until: string }
  | { kind: 'paused'; release_id: number; version: string | null; by_name: string | null }
  /** The source stage's arrival is not verified yet: `chip` in a word or two, `text` the step's `closed_reason`. */
  | { kind: 'verify'; chip: string; text: string }
  | { kind: 'press'; roles: string[]; users: Named[] }
  | {
    kind: 'approvals'; have: number; need: number; roles: string[]; users: Named[]
    approved: { name: string; comment: string | null; at: string }[]
    viewer: 'counts' | 'requester' | 'not_approver' | 'approved' | null; nobody: boolean
  }
  | { kind: 'order'; after_release_id: number; version: string | null }
  | { kind: 'soak'; until: string; source: string }
  | { kind: 'hours'; next: string; timezone: string }
  | { kind: 'sign_off'; count: number }
  | { kind: 'instructions'; text: string }
  | { kind: 'timeout'; at: string; action: 'cancel' | 'remind' }
  | { kind: 'freeze_soon'; name: string; starts: string }
export type PipelineView = {
  stages: ViewStage[]
  prod_live: ({ stage_id: string } & StageLive) | null
  steps: ViewStep[]
  not_started: number
  done: number
  archived: number
}

export type ItemChange = { sha: string; title: string; merged_at: string | null; feature: string | null }
export type PipelineItem = {
  key: string
  stage_id: string | null
  group: string | null
  task: PipelineTask | null
  change: ItemChange | null
  failure: string | null
  /** A hidden task listed for the change it merged: its stage folds it under Automatic. */
  automatic?: boolean
}

/** The buckets outside the stages (`membership.py::BUCKETS`), with their words, in the order they sit. */
export const BUCKETS = [
  ['not_started', 'Not started'],
  ['done', 'Done'],
  ['archived', 'Archived'],
] as const
export type Bucket = (typeof BUCKETS)[number][0]
export const isBucket = (value: string | null): value is Bucket => BUCKETS.some(([id]) => id === value)

/** `?stage=`: a stage id or a bucket (AC-5); `?only=`: an in-stage group a step's count picked (AC-6). */
export const STAGE_PARAM = 'stage'
export const ONLY_PARAM = 'only'
/** `?only=` on a release's source stage: its Release step, picked (`ReleasePick.tsx` draws the list). */
export const RELEASE_ONLY = 'release'

/** `{epic id: the child task ids a release ships}` (`TaskListViewProps.parts`). */
export type ReleaseParts = ReadonlyMap<number, readonly number[]>

/** A release's `parts` off the wire, keyed by epic id (`TaskListViewProps.parts`). */
export const releaseParts = (parts: Record<string, number[]> | undefined): ReleaseParts =>
  new Map(Object.entries(parts ?? {}).map(([epic, children]) => [Number(epic), children]))

/** The run of the release arriving on `toStageId` (its Release step's), as the view reports it. */
export function releaseRunTo(view: PipelineView | undefined, toStageId: string): ReleaseRun | null {
  return view?.steps.find(s => s.action === 'release' && s.to_stage_id === toStageId)?.release?.run ?? null
}

/** The stage a project opens on: the rightmost stage with a question, else with a failure, else with a stopped task,
 *  else the first stage (Ideas). */
export function attentionStage(view: Pick<PipelineView, 'stages'>): string | null {
  const rightmost = (has: (stage: ViewStage) => boolean) => [...view.stages].reverse().find(has)
  return (rightmost(s => s.asking > 0) ?? rightmost(s => s.failed > 0) ?? rightmost(s => (s.stopped ?? 0) > 0)
    ?? view.stages[0])?.id ?? null
}

/** The rail's navigation state when it opens a project: its `?scope=` key, so the board opens that project on its
 *  `attentionStage` once the strip is read. */
export const AUTO_STAGE_STATE = 'autoStage'

/** The in-stage groups (`pipeline/membership.py::GROUPS`) a release-target stage lists above its releases: what
 *  needs someone. */
export const ATTENTION_GROUPS: ReadonlySet<string> = new Set(['asking', 'failed'])

/** The in-stage groups a stage neither lists nor counts (`pipeline/membership.py::ON_STEP_ONLY`): its step's count
 *  picks them, across every stage. */
export const ON_STEP_ONLY: ReadonlySet<string> = new Set(['merging'])

/** The board filters the items read takes (`routes/tasks/listing.py`'s names). */
export type ItemFilters = {
  q?: string
  assigneeIds?: readonly string[]
  creatorIds?: readonly string[]
  sources?: readonly string[]
  state?: string | null
  after?: string | null
  before?: string | null
  focus?: boolean
  /** A feature's id: that feature's tasks alone. */
  featureId?: string | null
  persona?: string | null
  showHidden?: boolean
  /** The user's hidden nodes above the scope, as `lib/taskMute.ts::muteExclusionParams` names them: their tasks
   *  leave every count and list. */
  exclusions?: readonly (readonly [string, string])[]
}

/** `exclusions`' ids under one param name. */
const excluded = (f: ItemFilters, name: string) => (f.exclusions ?? []).filter(([k]) => k === name).map(([, v]) => v)

/** The same filters in `act`/`bulk`'s body, so a whole-stage verb covers what the list shows. */
export function filtersBody(f: ItemFilters): Record<string, unknown> {
  return {
    q: f.q || null,
    assigned_to_id: f.assigneeIds?.length ? f.assigneeIds : null,
    created_by_id: f.creatorIds?.length ? f.creatorIds : null,
    source: f.sources?.length ? f.sources : null,
    state: f.state || null,
    updated_after: f.after || null,
    updated_before: f.before || null,
    focus: !!f.focus,
    feature_id: f.featureId || null,
    persona: f.persona || null,
    exclude_feature_id: excluded(f, 'exclude_feature_id'),
  }
}

/** No filter narrows the read: its answer speaks for the whole board. */
export const isUnfiltered = (f: ItemFilters) => filtersQuery(f).toString() === ''

function filtersQuery(f: ItemFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (f.q) params.set('q', f.q)
  for (const id of f.assigneeIds ?? []) params.append('assigned_to_id', id)
  for (const id of f.creatorIds ?? []) params.append('created_by_id', id)
  for (const s of f.sources ?? []) params.append('source', s)
  if (f.state) params.set('state', f.state)
  if (f.after) params.set('updated_after', f.after)
  if (f.before) params.set('updated_before', f.before)
  if (f.focus) params.set('focus', 'true')
  if (f.featureId) params.set('feature_id', f.featureId)
  if (f.persona) params.set('persona', f.persona)
  if (f.showHidden) params.set('include_hidden', 'true')
  for (const [name, value] of f.exclusions ?? []) params.append(name, value)
  return params
}

/** The view under the list's filters: its counts are the listed rows'. */
export const pipelineViewKey = (projectId: number, filters: ItemFilters) =>
  ['pipeline-view', projectId, JSON.stringify(filters)] as const
export const pipelineItemsKey = (projectId: number) => ['pipeline-items', projectId] as const
export const PIPELINE_TOTALS_KEY = ['pipeline-totals'] as const

export const getPipelineView = (projectId: number, filters: ItemFilters) => {
  const query = filtersQuery(filters).toString()
  return get<PipelineView>(`/api/projects/${projectId}/pipeline/view${query ? `?${query}` : ''}`)
}

/** The rows one read asks for: the list loads this many, then as many again each time its end is reached. */
export const PAGE_LIMIT = 50
/** A release-target stage's read (the open release's rows with it), and All projects' per-project page
 *  (`board.py::ITEMS_LIMIT_MAX`). */
const ITEMS_LIMIT_MAX = 200

/** A release-target stage's first page: its items and its releases (`?stage=<id>`), under the list's filters (a
 *  narrowed list keeps only the releases that carried a listed item). */
export const getStageReleases = (projectId: number, stageId: string, filters: ItemFilters, signal?: AbortSignal) => {
  const params = filtersQuery(filters)
  params.set('stage', stageId)
  params.set('limit', String(ITEMS_LIMIT_MAX))
  return get<{ rows: PipelineItem[]; releases?: ReleaseFold[] }>(
    `/api/projects/${projectId}/pipeline/items?${params.toString()}`, { signal })
}

/** A folded release's items, by feature: those the list's filters keep. */
export const getReleaseItems = (projectId: number, releaseId: number, filters: ItemFilters) => {
  const query = filtersQuery(filters).toString()
  return get<{
    id: number; version: string; features: { name: string | null; items: string[] }[]; rows: PipelineItem[]
    run: ReleaseRun | null; parts?: Record<string, number[]>
  }>(
    `/api/projects/${projectId}/pipeline/releases/${releaseId}/items${query ? `?${query}` : ''}`)
}

/** The pages a re-read keeps: a list scrolled further starts over from these, so a refresh is never one read per
 *  page ever loaded. */
const REFRESH_PAGES = 4

/** Re-reads this project's items (and its releases' rows): each paged list first cut back to `REFRESH_PAGES` —
 *  unless `whole` (rows are picked: a pick on a later page must stay listed). */
export function refreshPipelineItems(queryClient: QueryClient, projectId: number, whole = false): void {
  if (!whole) {
    queryClient.setQueriesData<InfiniteData<unknown, unknown>>({ queryKey: pipelineItemsKey(projectId) }, data =>
      data && Array.isArray(data.pages) && data.pages.length > REFRESH_PAGES
        ? { pages: data.pages.slice(0, REFRESH_PAGES), pageParams: data.pageParams.slice(0, REFRESH_PAGES) }
        : data)
  }
  void queryClient.invalidateQueries({ queryKey: pipelineItemsKey(projectId) })
}

/** What one items read picks: a stage id or a bucket (absent: every stage), and an in-stage group. */
export type ItemsPick = { stage: string | null; group: string | null }

/** One page of items in screen order, how many the pick holds in all, and the cursor of the next (`null` at the end). */
export async function getPipelineItemsPage(
  projectId: number,
  pick: ItemsPick,
  filters: ItemFilters,
  after: string | null,
  signal?: AbortSignal,
): Promise<{ rows: PipelineItem[]; total: number; folded: Record<string, number>; next: string | null }> {
  const params = filtersQuery(filters)
  if (pick.stage) params.set('stage', pick.stage)
  if (pick.group) params.set('group', pick.group)
  params.set('limit', String(PAGE_LIMIT))
  if (after) params.set('after', after)
  const { data, next } = await getPage<{ rows: PipelineItem[]; total: number; folded?: Record<string, number> }>(
    `/api/projects/${projectId}/pipeline/items?${params.toString()}`, { signal })
  return { rows: data.rows, total: data.total, folded: data.folded ?? {}, next }
}

export type Subjects = {
  task_ids?: number[]
  all_in_stage?: boolean
  /** With `all_in_stage`: only the stage's items in this group. */
  group?: string
  /** With `all_in_stage`: false leaves out what the list folds under Automatic (its fold is closed). */
  folded?: boolean
  stage?: string
  filters?: Record<string, unknown>
  include_hidden?: boolean
}
/** The most items one verb names (`pipeline/actions.py::SUBJECTS_MAX`). */
export const SUBJECTS_MAX = 1000
export type ActOutcome = { done: number; refused: { key: string; reason: string }[]; waiting: number }

/** `comment`: an approver's note on Approve or Reject; `overrideReason`: a press run through a freeze. Each is sent
 * only when written. */
export const actOnStep = (
  projectId: number, toStageId: string, verb: 'go' | 'approve' | 'reject', subjects: Subjects,
  opts: { comment?: string; overrideReason?: string } = {},
) =>
  post<ActOutcome>(`/api/projects/${projectId}/pipeline/steps/${toStageId}/act`, {
    verb, subjects, ...(opts.comment ? { comment: opts.comment } : {}),
    ...(opts.overrideReason ? { override_reason: opts.overrideReason } : {}),
  })
/** The freeze holding a step, as its first reason names it: "Weekend", or "Freeze now"; null when none holds it. */
export function freezeHolding(step: Pick<ViewStep, 'reasons'> | undefined): string | null {
  const first = step?.reasons?.[0]
  if (first?.kind === 'freeze') return first.name
  if (first?.kind === 'freeze_now') return 'Freeze now'
  return null
}

/** Roll a stage back to the version before its failed arrival: a rollback release (201), or 409 `open` / `not_offered`. */
export const rollbackStage = (projectId: number, stageId: string) =>
  post<unknown>(`/api/projects/${projectId}/stages/${stageId}/rollback`)

export const bulkItems = (projectId: number, verb: 'archive' | 'restore' | 'delete_drafts', subjects: Subjects) =>
  post<ActOutcome>(`/api/projects/${projectId}/pipeline/bulk`, { verb, subjects })

/** All projects (secondary, D7): stage totals by name, under the list's filters. */
export type PipelineTotals = { totals: { name: string; count: number }[] }
export const pipelineTotalsKey = (filters: ItemFilters) => [...PIPELINE_TOTALS_KEY, JSON.stringify(filters)] as const
export const getPipelineTotals = (filters: ItemFilters) => {
  const query = filtersQuery(filters).toString()
  return get<PipelineTotals>(`/api/pipeline/view${query ? `?${query}` : ''}`)
}

/** All projects with a stage total picked (by name, any case): each project's first page of that stage's items. */
export type ProjectStageItems = { project_id: number; name: string; rows: PipelineItem[]; total: number }
export const PIPELINE_ALL_ITEMS_KEY = ['pipeline-all-items'] as const
export const pipelineAllItemsKey = (stageName: string, filters: ItemFilters) =>
  [...PIPELINE_ALL_ITEMS_KEY, stageName, JSON.stringify(filters)] as const
export const getAllStageItems = (stageName: string, filters: ItemFilters, signal?: AbortSignal) => {
  const params = filtersQuery(filters)
  params.set('stage', stageName)
  params.set('limit', String(ITEMS_LIMIT_MAX))
  return get<{ projects: ProjectStageItems[] }>(`/api/pipeline/items?${params.toString()}`, { signal })
}

/** The tasks a stage's items across projects carry, in each project's screen order, and how many of the stage's items
 *  they leave out (later pages, and changes merged without a task, which only a project's own list shows). */
export function stageTasksAcross(projects: readonly ProjectStageItems[]): { tasks: PipelineTask[]; left: number } {
  const tasks: PipelineTask[] = []
  let left = 0
  for (const project of projects) {
    let shown = 0
    for (const row of project.rows) {
      if (row.task) {
        tasks.push(row.task)
        shown += 1
      }
    }
    left += Math.max(0, project.total - shown)
  }
  return { tasks, left }
}

/** The totals' own name for the stage `name` names (any case), or null when they hold none. */
export const totalsStage = (totals: PipelineTotals['totals'], name: string): string | null =>
  totals.find(total => total.name.toLowerCase() === name.toLowerCase())?.name ?? null

/** A step's forward verb on the stage it leaves; none for a release (its buttons are the release's own). */
export function forwardVerb(step: ViewStep | undefined, next: ViewStage | undefined): string | null {
  if (!step || !next || step.action === 'release') return null
  if (step.action === 'start' || step.action === 'start_merge') return 'Start'
  if (step.action === 'merge') return 'Merge'
  return `Move to ${next.name}`
}

/** One outcome as words: "Started 20", plus what waits and what was refused. */
export function outcomeWords(done: string, outcome: ActOutcome): string {
  const parts = [`${done} ${outcome.done}`]
  if (outcome.waiting) parts.push(`${outcome.waiting} waiting`)
  if (outcome.refused.length) parts.push(`${outcome.refused.length} refused`)
  return parts.join(' · ')
}
