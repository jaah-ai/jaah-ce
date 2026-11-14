import { del, get, post, put } from '../api'
import type { Page } from './productionQa'
import { fmtTimestamp } from './relativeTime'
import type { Freeze, FreezeIn } from './releases'
import type { Gate, GateIn, Target } from './pipelineGates'
import type { PaletteKey } from '../theme'

/* PIPELINE SETUP — the web half of `GET`/`PUT /api/projects/{id}/pipeline` (`apps/api/app/releases/pipeline.py`):
 * the project's stages (name, branch, address — identical for every stage) and the step into each (who, approvals,
 * by hand or by itself; its action derived, never edited). `steps[i]` is the step into `stages[i + 1]`. */

export type StepAction = 'move' | 'start' | 'start_merge' | 'merge' | 'release'
export type Level = 'none' | 'work' | 'branch'
export type Who = { roles: string[]; users: string[] }

/** A step's allowed hours: weekdays 0 = Monday … 6 = Sunday, "HH:MM" (`to` before `from` runs overnight), IANA zone. */
export type AllowedHours = { days: number[]; from: string; to: string; timezone: string }
/** A step's rules (`apps/api/app/pipeline/rules.py`), each defaulting to today's behaviour (`DEFAULT_RULES`). */
export type StepRules = {
  /** null: the same as who may press. */
  approvers: Who | null
  requester_cant_approve: boolean
  /** null: no timeout; else 15..43200. */
  approval_timeout_minutes: number | null
  on_timeout: 'cancel' | 'remind'
  /** At most 1,000 characters. */
  instructions: string
  /** 0..43200. */
  soak_minutes: number
  /** null: any time. */
  allowed_hours: AllowedHours | null
  /** Release steps only: anything else is refused unless `newest`. */
  concurrency: 'newest' | 'in_order'
}
export const DEFAULT_RULES: StepRules = {
  approvers: null, requester_cant_approve: true, approval_timeout_minutes: null, on_timeout: 'cancel',
  instructions: '', soak_minutes: 0, allowed_hours: null, concurrency: 'newest',
}
/** A step's rules as read, so a save sends them back unchanged (a rule left out is reset to its default). */
export const rulesOf = (step: Partial<StepRules> | undefined): StepRules => ({
  approvers: step?.approvers ?? DEFAULT_RULES.approvers,
  requester_cant_approve: step?.requester_cant_approve ?? DEFAULT_RULES.requester_cant_approve,
  approval_timeout_minutes: step?.approval_timeout_minutes ?? DEFAULT_RULES.approval_timeout_minutes,
  on_timeout: step?.on_timeout ?? DEFAULT_RULES.on_timeout,
  instructions: step?.instructions ?? DEFAULT_RULES.instructions,
  soak_minutes: step?.soak_minutes ?? DEFAULT_RULES.soak_minutes,
  allowed_hours: step?.allowed_hours ?? DEFAULT_RULES.allowed_hours,
  concurrency: step?.concurrency ?? DEFAULT_RULES.concurrency,
})
/** A step paused by itself after an off-path release. */
export type StepPause = { release_id: number; version: string | null; by_name: string | null; at: string }

/** Where a stage's address comes from (its deploy target, `release_stages.target_kind`; `lib/pipelineGates.ts`). */
export type { TargetKind } from './pipelineGates'
/** A stage address's last check (`app/releases/health.py`): null on a read means the stage has no address. */
export type StageHealth = { state: 'healthy' | 'down' | 'unchecked'; checked_at: string | null; detail: string | null }
export type PipelineStage = {
  id: string; name: string; branch: string; url: string | null; level: Level
  /** Where `url` came from (`models/releases.py::URL_SOURCES`); null when none is set. */
  url_source?: string | null
  /** The stage's deploy target and its After-arrival gates (`lib/pipelineGates.ts`); absent from an older server. */
  target?: Target
  after?: Gate[]
  health?: StageHealth | null
}
export type PipelineStep = StepRules & {
  to_stage_id: string
  action: StepAction
  /** null: the action's default, which `who_effective` resolves. */
  who: Who | null
  who_effective: Who
  approvals: number
  auto: boolean
  /** The step's gates, run in order before a change goes into its stage (merge and release steps; else empty). */
  gates?: Gate[]
  /** `approvers`, or with none set who may press. */
  approvers_effective: Who
  pause: StepPause | null
}
export type FreezeNow = { by_name: string | null; at: string }
export type Pipeline = {
  stages: PipelineStage[]
  steps: PipelineStep[]
  freezes: Freeze[]
  /** Freeze now: on since `at`, or null when off. */
  freeze_now: FreezeNow | null
  release_running: boolean
  /** Each action's default roles: what `who: null` resolves to once a draft edit changes a step's action. */
  defaults: Record<StepAction, string[]>
  /** The named users' names, by id. */
  names: Record<string, string>
  /** Each org pipeline rule the pipeline breaks (`releases/guardrails.py`): on a read, the stored one's; on a save,
   *  the rules set to Warn (one set to Block refuses the save with 422 `guardrail` instead). */
  warnings: PipelineWarning[]
  /** The org pipeline template this pipeline is linked to, or null. */
  template: PipelineTemplateLink | null
}
/** A linked template's version and, by index with `stages`/`steps`, which fields it locks and the values it sets
 *  (`values.stages[i].branch` already resolved to this project's branch). */
export type PipelineTemplateLink = {
  id: string
  name: string
  version_id: string
  version_number: number
  published_at: string | null
  locks: { stages: { branch: boolean; url: boolean }[]; steps: { who: boolean; approvals: boolean; auto: boolean }[] }
  values: {
    stages: { name: string; branch: string; url: string | null }[]
    steps: { who: Who | null; approvals: number; auto: boolean }[]
  }
}
export type PipelineWarning = { rule: string; stage_index: number; message: string }
export type PipelineIn = {
  /** `url`, `target` and `after` left out: kept as stored. */
  stages: { id?: string; name: string; branch: string; url?: string | null; target?: Target; after?: GateIn[] }[]
  /** `gates` left out: kept as stored. */
  steps: ({ who: Who | null; approvals: number; auto: boolean; gates?: GateIn[] } & StepRules)[]
  /** Sent: the freezes are replaced in the same save (one History row). Absent: kept as stored. */
  freezes?: FreezeIn[]
}
/** One save in Project settings › Pipeline › History (`releases/history.py`): who, when, what changed. */
export type PipelineRevision = {
  id: number
  kind: 'save' | 'restore'
  restored_from_id: number | null
  created_at: string
  created_by: { id: string; name: string } | null
  changes: string[]
}

/** What an unsaved draft would change for work already on the pipeline (`releases/impact.py`). */
export type PipelineImpact =
  | { kind: 'stage_removed'; stage: string; count: number }
  | { kind: 'next_stage'; stage: string; count: number; to: string }
  | { kind: 'approvals'; stage: string; count: number; to: string; action: StepAction; before: number; after: number }
  | { kind: 'auto'; stage: string; count: number; to: string; action: StepAction; after: boolean }
  | { kind: 'who'; stage: string; count: number; to: string; action: StepAction }
  | { kind: 'release_frozen'; stage: string; at: string; release_id: number; freeze: string }
  | { kind: 'release_unfrozen'; stage: string; at: string; release_id: number }

/** The API's own cap on approvals (`releases/models.py::APPROVALS_MAX`). */
export const APPROVALS_MAX = 5

export const pipelineKey = (projectId: number) => ['pipeline', projectId] as const
export const getPipeline = (projectId: number) => get<Pipeline>(`/api/projects/${projectId}/pipeline`)
export const putPipeline = (projectId: number, body: PipelineIn) => put<Pipeline>(`/api/projects/${projectId}/pipeline`, body)
export const previewPipeline = (projectId: number, body: PipelineIn) =>
  post<{ impacts: PipelineImpact[] }>(`/api/projects/${projectId}/pipeline/preview`, body)
export const pipelineHistoryKey = (projectId: number) => ['pipeline-history', projectId] as const
export const getPipelineHistory = (projectId: number, cursor?: string | null) =>
  get<Page<PipelineRevision>>(`/api/projects/${projectId}/pipeline/history?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
export const restorePipeline = (projectId: number, revisionId: number) =>
  post<Pipeline>(`/api/projects/${projectId}/pipeline/history/${revisionId}/restore`)
export type FreezeNowOut = { on: boolean; by_name: string | null; at: string | null }
export const putFreezeNow = (projectId: number, on: boolean) =>
  put<FreezeNowOut>(`/api/projects/${projectId}/release/freeze-now`, { on })

/** The level a typed branch implies (`releases/stages.py::level_for`): empty → none, a `*` pattern → work, else branch. */
export function levelFor(branch: string): Level {
  const text = branch.trim()
  if (!text) return 'none'
  return text.includes('*') ? 'work' : 'branch'
}

const RANK: Record<Level, number> = { none: 0, work: 1, branch: 2 }

/** What the step into stage `index` does (`releases/pipeline.py::action`), from the stages' branches. */
export function actionAt(levels: readonly Level[], index: number): StepAction {
  const level = levels[index]
  if (level === 'none') return 'move'
  if (level === 'work') return 'start'
  const highest = Math.max(0, ...levels.slice(0, index).map(l => RANK[l]))
  return highest === 0 ? 'start_merge' : highest === 1 ? 'merge' : 'release'
}

export const ACTION_LABEL: Record<StepAction, string> = {
  move: 'Move', start: 'Start', start_merge: 'Start and merge', merge: 'Merge', release: 'Release',
}

/** Each step's colour on the strip (`StageStrip.tsx`, `ReleaseStep.tsx`): violet Start, green Merge, blue Release;
 *  Move stays grey. */
export const STEP_ACCENT: Record<StepAction, PaletteKey | null> = {
  move: null, start: 'secondary', start_merge: 'success', merge: 'success', release: 'primary',
}

/** The four roles a step's `who` may name (`app/access_catalog.py::ROLES`), in the Who picker's order. */
export const STEP_ROLES: readonly { value: string; label: string }[] = [
  { value: 'org_owner', label: 'Org Owner' },
  { value: 'org_admin', label: 'Org Admin' },
  { value: 'project_maintainer', label: 'Maintainer' },
  { value: 'project_developer', label: 'Developer' },
]

export const ROLE_LABEL = new Map(STEP_ROLES.map(r => [r.value, r.label]))
/** A role slug as the Who picker names it ("Maintainer"); Viewer is not offered there but may be stored. */
export const roleLabel = (slug: string): string => ROLE_LABEL.get(slug) ?? (slug === 'project_viewer' ? 'Viewer' : slug)

const items = (n: number) => `${n} ${n === 1 ? 'item' : 'items'}`

/** One impact as the sentence the preview shows: "3 items in Building will now need 2 approvals to merge." */
export function impactText(impact: PipelineImpact): string {
  if (impact.kind === 'release_frozen') {
    return `The release to ${impact.stage} at ${fmtTimestamp(impact.at)} falls inside the new freeze ${impact.freeze}.`
  }
  if (impact.kind === 'release_unfrozen') {
    return `The release to ${impact.stage} at ${fmtTimestamp(impact.at)} is no longer held by a freeze.`
  }
  const where = `${items(impact.count)} in ${impact.stage}`
  if (impact.kind === 'stage_removed') return `${where} lose their stage.`
  if (impact.kind === 'next_stage') return `${where} will go to ${impact.to} next.`
  const verb = ACTION_LABEL[impact.action].toLowerCase()
  if (impact.kind === 'approvals') {
    return impact.after === 0 ? `${where} will no longer need approvals to ${verb}.`
      : `${where} will now need ${impact.after} ${impact.after === 1 ? 'approval' : 'approvals'} to ${verb}.`
  }
  if (impact.kind === 'auto') {
    return impact.after ? `${where} will ${verb} by themselves.` : `${where} will wait for someone to press ${ACTION_LABEL[impact.action]}.`
  }
  return `${where}: who may ${verb} them changes.`
}

/** What the API would refuse in these stages (`releases/pipeline.py::validate`), as one message per stage index — so
 *  the card says it as you type, not only after Save. The server stays the authority; this mirrors its rules: a
 *  name, unique names, a work pattern ending in its one `/*`, one work stage, before every branch stage, and unique
 *  branch names. */
export function stageProblems(stages: readonly { name: string; branch: string }[]): Map<number, string> {
  const problems = new Map<number, string>()
  const flag = (index: number, message: string) => { if (!problems.has(index)) problems.set(index, message) }
  const names = new Map<string, number>()
  const branches = new Map<string, number>()
  let work: number | null = null
  let firstBranch: number | null = null
  const nameOf = (index: number) => stages[index].name.trim() || `Stage ${index + 1}`
  stages.forEach((stage, index) => {
    const name = stage.name.trim()
    const key = name.toLowerCase()
    if (!key) flag(index, 'Name required.')
    else if (names.has(key)) flag(index, `Two stages are named ${name}.`)
    else names.set(key, index)
    const label = nameOf(index)
    const text = stage.branch.trim()
    const level = levelFor(text)
    if (level === 'work') {
      if ((text.match(/\*/g) ?? []).length !== 1 || !(text === '*' || text.endsWith('/*'))) {
        flag(index, `${label}: a work branch is a prefix ending in /*, like fleet/*.`)
      }
      if (work !== null) flag(index, `Only one stage can build on work branches: ${nameOf(work)} or ${label}.`)
      else if (firstBranch !== null) flag(index, `${label} must come before ${nameOf(firstBranch)}.`)
      if (work === null) work = index
    } else if (level === 'branch') {
      const other = branches.get(text)
      if (other !== undefined) flag(index, `${nameOf(other)} and ${label} both use ${text}.`)
      else branches.set(text, index)
      if (firstBranch === null) firstBranch = index
    }
  })
  return problems
}

/* STARTING FROM — presets and copy from project (`apps/api/app/releases/pipeline_doc.py`): a source fills the editor as
 * a draft (`postPipelineDraft`), never saves; the person reviews it and presses Save. */

export type PipelinePreset = { slug: string; name: string; note: string; chain: string }
export type PipelineChain = { project_id: number; name: string; chain: string }
export type PipelineChange = {
  kind: 'stage_added' | 'stage_removed' | 'stage_renamed' | 'stage_moved' | 'field' | 'lock'
  stage: string
  field?: string
  old?: unknown
  new?: unknown
}
export type PipelineProblem = { code: string; message: string; stage_index?: number }
export type PipelineDraft = { draft: PipelineIn; changes: PipelineChange[]; problems: PipelineProblem[] }
export type DraftSource = { preset: string } | { project_id: number }

/** A new business initiative starts from it; the one preset the server lets a project without a repository take. */
export const IDEAS_ONLY_PRESET = 'ideas_only'

export const presetsKey = ['pipeline-presets'] as const
export const getPresets = () => get<PipelinePreset[]>('/api/pipeline/presets')
export const chainsKey = (projectId: number) => ['pipeline-chains', projectId] as const
export const getPipelineChains = (projectId: number) => get<PipelineChain[]>(`/api/projects/pipeline-chains?exclude=${projectId}`)
export const postPipelineDraft = (projectId: number, source: DraftSource) =>
  post<PipelineDraft>(`/api/projects/${projectId}/pipeline/draft`, { source })

/* LINKING TO AN ORG TEMPLATE: preview what linking changes (writes nothing), link (409 `template_unpublished`, 422
 * `guardrail`), unlink (values kept, every field editable). A linked project's draft and locked-field saves are refused. */
export type TemplatePreview = {
  template: { id: string; name: string; version_number: number; published_at: string | null }
  changes: PipelineChange[]
  problems: PipelineProblem[]
}
export const getTemplatePreview = (projectId: number, templateId: string) =>
  get<TemplatePreview>(`/api/projects/${projectId}/pipeline/template-preview?template_id=${encodeURIComponent(templateId)}`)
export const linkTemplate = (projectId: number, templateId: string) =>
  put<Pipeline>(`/api/projects/${projectId}/pipeline/template`, { template_id: templateId })
export const unlinkTemplate = (projectId: number) => del<Pipeline>(`/api/projects/${projectId}/pipeline/template`)

const FIELD_LABEL: Record<string, string> = { branch: 'Branch', url: 'Address', who: 'Who', approvals: 'Approvals', auto: 'How it runs' }

function valueText(field: string | undefined, value: unknown, roleLabel: (slug: string) => string): string {
  if (field === 'auto') return value ? 'By itself' : 'By hand'
  if (field === 'who') {
    if (value === null || value === undefined) return 'Default'
    const who = value as Who
    const names = [...who.roles.map(roleLabel), ...(who.users.length ? [`${who.users.length} user${who.users.length === 1 ? '' : 's'}`] : [])]
    return names.length ? names.join(', ') : 'No one'
  }
  if (field === 'branch') {
    if (value === '' || value === null || value === undefined) return 'None'
    if (value === '*') return 'work branches'
    if (value === '$integration') return 'integration branch'
  }
  if (value === null || value === undefined || value === '') return 'None'
  return String(value)
}

/** One change in plain words: "+ Staging", "− Dev", "Production · Approvals: 0 → 1". */
export function changeText(change: PipelineChange, roleLabel: (slug: string) => string = s => s): string {
  switch (change.kind) {
    case 'stage_added': return `+ ${change.stage}`
    case 'stage_removed': return `− ${change.stage}`
    case 'stage_renamed': return `${String(change.old)} renamed to ${String(change.new)}`
    case 'stage_moved': return `${change.stage} moves`
    case 'lock': return `${change.stage} · ${FIELD_LABEL[change.field ?? ''] ?? change.field} ${change.new ? 'locked' : 'unlocked'}`
    default: {
      const label = FIELD_LABEL[change.field ?? ''] ?? change.field
      return `${change.stage} · ${label}: ${valueText(change.field, change.old, roleLabel)} → ${valueText(change.field, change.new, roleLabel)}`
    }
  }
}
