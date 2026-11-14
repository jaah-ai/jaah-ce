import { get, post } from '../api'
import type { Tone } from '../ui/StatusChip'
import { shortDate } from './relativeTime'

/* RELEASES — the web half of the pinned release seam (`apps/api/app/releases/views.py`, routes
 * `apps/api/app/routes/releases.py`): the project's release read, a Release, the status vocabulary every
 * surface draws (the pipeline page's Release step, the Easy Mode panel), and the writes. Shapes mirror the API;
 * nothing here re-derives a server decision (`can_*`, `next_action`, `status`). */

export type Person = { id: string; name: string }

export type Change = {
  change_id: string
  title: string
  author: Person | null
  merged_at: string | null
  task_id: number | null
  pr_url: string | null
  /** The top-level task the board lists for the change: an epic for its child's merge. */
  root_task_id?: number | null
}

/** The task a change's row is: its top-level task, as the board lists it; null for a change with no task. */
export const changeRow = (change: Change): number | null => change.root_task_id ?? change.task_id

/** How many tasks `changes` are, as the board lists them: a task merged twice, or an epic's children, is one; a
 *  change with no task counts alone. A step's coin counts these, so it matches its stage's task count. */
export function taskCount(changes: readonly Change[]): number {
  return new Set(changes.map(c => changeRow(c) ?? c.change_id)).size
}

export type ReleaseState =
  | 'awaiting_approval' | 'scheduled' | 'queued' | 'merging' | 'deploying' | 'live' | 'shipped' | 'failed' | 'canceled'

export type FailureAction = 'retry' | 'connect' | 'open_pr' | 'open_run'

export type Release = {
  id: number
  kind: 'promote' | 'pick' | 'hotfix' | 'backmerge'
  from_stage_id: string | null
  to_stage_id: string | null
  state: ReleaseState
  requested_by: Person | null
  requested_at: string | null
  scheduled_for: string | null
  approvals: { user: Person | null; at: string | null; comment: string | null }[]
  approvals_required: number
  can_approve: boolean
  can_reject: boolean
  /** What an approver is asked to check; "" for none. */
  instructions: string
  /** Why a canceled release ended: timed out, rejected (by whom, what they said), or superseded by a newer release. */
  cancellation: {
    kind: 'timeout' | 'rejected' | 'superseded' | 'manual'; by?: Person | null; comment?: string | null
    by_release_id?: number | null
  } | null
  can_cancel: boolean
  pr_url: string | null
  merge_sha: string | null
  head_sha: string | null
  commits: number | null
  deployment: { id: number; run_url: string | null; status: string; finished_at: string | null } | null
  /** `url`: the page the action opens when the release does not hold it — the PR of a hotfix's change (`open_pr`), or
   * the run of a deploy no recorded run carries (`open_run`). */
  failure: { kind?: string; message?: string; action?: FailureAction; url?: string } | null
  /** The release's history line for a freeze override (R7.5): who ran it through which freeze, when, and why. */
  freeze_override?: { by: Person | null; at: string | null; reason: string; freeze_name: string } | null
  /** Shipped without the full gate on a person's reason (`untested_reason` on Release). */
  untested?: { by: Person | null; at: string | null; reason: string } | null
  changes: Change[]
  /** The required gate that failed its newest gate run (a failed release only): "Failed at gate › <name>". */
  failed_gate?: string | null
  /** The ticket typed when the step was pressed, for its "ask when pressed" ticket gate. */
  ticket?: string | null
}

export type Deployers = { roles: string[]; users: Person[] }

export type Stage = {
  id: string
  position: number
  name: string
  branch: string | null
  url: string | null
  url_source: string | null
  deploy_environment: string | null
  approvals_required: number
  deployers: Deployers | null
  auto_deploy: boolean
  current: Release | null
  next_action: 'deploy' | 'approve' | null
  frozen_until: string | null
  /** Freeze now holds it: frozen with no end until a person switches it off. */
  frozen_now?: boolean
  /** The open release that fixes this stage's branch and environment and keeps it, until it ends. */
  locked_by: number | null
  /** The ticket system the step into this stage asks a ticket of when pressed, else null. */
  asks_ticket?: 'jira' | 'servicenow' | null
  /** The Jira project key of the login that ticket is asked through (OAM in OAM-123), for the field's example. */
  ticket_project?: string | null
}

export type Freeze = {
  id: string
  name: string
  starts_at: string
  ends_at: string
  /** The old spelling of `repeat === 'weekly'`. */
  weekly: boolean
  timezone: string
  repeat: FreezeRepeat
  /** `after_integration`: every stage after the integration stage; `stages`: those in `stage_ids`. */
  scope: FreezeScope
  stage_ids: string[]
  /** A step a person pressed runs during the freeze. */
  let_pressed: boolean
  /** A release already queued when the freeze began continues. */
  let_queued: boolean
}
export type FreezeRepeat = 'never' | 'daily' | 'weekly' | 'monthly'
export type FreezeScope = 'after_integration' | 'stages'

export type ReleaseTab = {
  stages: Stage[]
  freezes: Freeze[]
  waiting: Record<string, Change[]>
  extra_commits: Record<string, number | null>
  open: Release[]
  can_deploy: boolean
  /** `wait_reason`: why the queued executor has not started (its task page's sentence), null when nothing holds it. */
  executor: { state: 'idle' | 'queued' | 'running'; node_online: boolean; task_id: number | null; wait_reason: string | null }
  deploy_events: 'connected' | 'not_connected' | 'easy_mode'
  /** What the next promote out of the integration branch ships with the merge lane on: the commit the lane tested green
   *  furthest along it (`sha`, null when none yet), on `stage_id`'s step, how many lane merges came after it, and the
   *  step's waiting changes it does not contain (`newer_change_ids`: a whole promote leaves them waiting). */
  promote_pin: PromotePin | null
}

export type PromotePin = {
  stage_id: string; sha: string | null; certified_at: string | null; behind: number | null; newer_change_ids?: string[]
  /** Why the pin lags the branch, null when nothing is behind it: the newest full check found the branch red (`red_sha`,
   *  `failing` tests), a full check is running (`check_task_id`), or the next one is not filed yet. `since`: when the
   *  pinned commit was the branch's head. */
  stall?: PinStall | null
}

export type PinStall = {
  reason: 'red' | 'checking' | 'waiting'; since: string; red_sha: string | null; failing: number | null
  check_task_id: number | null
}

/** Why a whole promote's tested commit lags the branch, in a few words: "full check fails at a67a0a4 (3 failing)". */
export function stallWhy(stall: PinStall): string {
  if (stall.reason === 'checking') return 'full check running'
  if (stall.reason === 'waiting') return 'no full check filed yet'
  const at = stall.red_sha ? ` at ${stall.red_sha.slice(0, 7)}` : ''
  return `full check fails${at}${stall.failing ? ` (${stall.failing} failing)` : ''}`
}

/** The same as one line under the untested changes: "Untested since Nov 13, 12:44 PM: …". */
export function stallLine(stall: PinStall, now: Date = new Date()): string {
  return `Untested since ${shortDate(stall.since, now, true)}: ${stallWhy(stall)}`
}

/** A release state as one word. */
export type StatusWord =
  | 'up_to_date' | 'awaiting_approval' | 'scheduled' | 'queued' | 'deploying' | 'live' | 'deployed'
  | 'failed' | 'frozen'

type Page<T> = { items: T[]; next_cursor: string | null }

/** The one word and tone each status reads as, everywhere. */
export const STATUS: Record<StatusWord, { label: string; tone: Tone; glyph: string }> = {
  up_to_date: { label: 'Up to date', tone: 'neutral', glyph: '✓' },
  awaiting_approval: { label: 'Approval', tone: 'warning', glyph: '?' },
  scheduled: { label: 'Scheduled', tone: 'info', glyph: '◷' },
  queued: { label: 'Queued', tone: 'info', glyph: '○' },
  deploying: { label: 'Releasing', tone: 'warning', glyph: '»' },
  live: { label: 'Live', tone: 'success', glyph: '✓' },
  deployed: { label: 'Released', tone: 'success', glyph: '✓' },
  failed: { label: 'Failed', tone: 'error', glyph: '✕' },
  frozen: { label: 'Frozen', tone: 'neutral', glyph: '■' },
}

/** A release's state as the status word: `merging` reads as Releasing, `shipped` as Released. */
export function statusOfRelease(state: ReleaseState): StatusWord | null {
  switch (state) {
    case 'merging': return 'deploying'
    case 'shipped': return 'deployed'
    case 'canceled': return null
    default: return state
  }
}

/** A canceled release's words: "Superseded by #N" when a newer release replaced it, else Canceled. */
export function canceledLabel(release: Pick<Release, 'cancellation'>): string {
  const by = release.cancellation?.kind === 'superseded' ? release.cancellation.by_release_id : null
  return by != null ? `Superseded by #${by}` : 'Canceled'
}

/** The open release's words: what it waits for, or that it runs; a canceled one's, why it ended. */
export function inFlightLabel(release: Release): string {
  const prefix = release.kind === 'hotfix' ? 'Hotfix · ' : release.untested ? 'Untested · ' : ''
  if (release.state === 'canceled') return `${prefix}${canceledLabel(release)}`
  if (release.state === 'awaiting_approval') return `${prefix}${release.approvals.length}/${release.approvals_required} approvals`
  if (release.state === 'scheduled' && release.scheduled_for) return `${prefix}Scheduled ${shortDate(release.scheduled_for, new Date(), true)}`
  const word = statusOfRelease(release.state)
  return `${prefix}${word ? STATUS[word].label : release.state}`
}

/** "Release to production", "Release to staging": the verb names the target stage. */
export const deployLabel = (target: Pick<Stage, 'name'>): string => `Release to ${target.name.toLowerCase()}`

export const releaseKey = (projectId: number) => ['release', projectId] as const
/** How often a tab with something in flight re-reads. */
export const RELEASE_POLL_MS = 10_000
/** How often an idle tab re-reads while visible: a change merged meanwhile offers its Release without a reload. */
export const RELEASE_IDLE_POLL_MS = 30_000

const base = (projectId: number) => `/api/projects/${projectId}/release`

export const getReleaseTab = (projectId: number) => get<ReleaseTab>(base(projectId))
/** Ship what waits on `fromStageId`: everything (extra commits too), or only `changeIds` (a pick, `KIND_PICK`). */
export const deploy = (projectId: number, fromStageId: string, scheduledFor?: string | null, changeIds?: readonly string[],
  overrideReason?: string, ticket?: string | null, untestedReason?: string) =>
  post<Release>(`${base(projectId)}/deploy`, {
    from_stage_id: fromStageId, ...(scheduledFor ? { scheduled_for: scheduledFor } : {}),
    ...(changeIds ? { change_ids: changeIds } : {}), ...(overrideReason ? { override_reason: overrideReason } : {}),
    ...(ticket ? { ticket } : {}), ...(untestedReason ? { untested_reason: untestedReason } : {}),
  })
/** Ask for a fresh count of what each stage's branch holds (`tick.survey_on_look`): commits merged on the forge. */
export const requestSurvey = (projectId: number) => post<{ filed: boolean }>(`${base(projectId)}/survey`)
export const hotfix = (projectId: number, changeId: string) => post<Release>(`${base(projectId)}/hotfix`, { change_id: changeId })
/** `comment`: the approver's note, sent only when written. */
export const approve = (projectId: number, id: number, comment?: string) =>
  post<Release>(`${base(projectId)}/${id}/approve`, comment ? { comment } : {})
export const reject = (projectId: number, id: number, comment?: string) =>
  post<Release>(`${base(projectId)}/${id}/reject`, comment ? { comment } : {})
export const cancel = (projectId: number, id: number) => post<Release>(`${base(projectId)}/${id}/cancel`)
export const retry = (projectId: number, id: number, overrideReason?: string) =>
  overrideReason
    ? post<Release>(`${base(projectId)}/${id}/retry`, { override_reason: overrideReason })
    : post<Release>(`${base(projectId)}/${id}/retry`)

/** The shortest reason a freeze override takes (`apps/api/app/releases/override.py::REASON_MIN`). */
export const OVERRIDE_REASON_MIN = 10

/** A freeze as saved: a field left out keeps the stored value (an older form sends only `weekly`). A 422 names its
 *  `field` and `index` (`window_too_long`, `stages_required`, `stage_unknown`). */
export type FreezeIn = Pick<Freeze, 'name' | 'starts_at' | 'ends_at' | 'weekly' | 'timezone'>
  & Partial<Pick<Freeze, 'repeat' | 'scope' | 'stage_ids' | 'let_pressed' | 'let_queued'>> & { id?: string }


export const releaseHistory = (projectId: number) => get<Page<Release>>(`${base(projectId)}/history?limit=20`)

/** The newest release per target stage, from a newest-first history page. */
export function latestByStage(history: readonly Release[]): Map<string, Release> {
  const out = new Map<string, Release>()
  for (const r of history) if (r.to_stage_id && !out.has(r.to_stage_id)) out.set(r.to_stage_id, r)
  return out
}

/** The newest open release into `stageId`: several may wait into one stage (oldest first on the wire), and the step's
 *  verbs act on the newest, as the board shows it. */
export function openInto(open: Release[], stageId: string): Release | undefined {
  return [...open].reverse().find(r => r.to_stage_id === stageId)
}

/** An ISO instant as a `datetime-local` value in the viewer's zone, and back. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
export function fromLocalInput(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}
