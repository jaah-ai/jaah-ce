// The Automations surface's API client — `apps/api/app/routes/automations.py`, field for field.
// One Automation is When -> Target -> Action over a schedule (cron) or event (trigger) child; nothing is
// recomputed here: status, its reason, the schedule sentence and next run are the server's.

import { del, get, patch, post, postForm } from '../../api'
import { RUNS_PAGE_SIZE } from '../../hooks/usePagedList'
import type { TargetDangling, TargetId, TargetKind, TargetRule } from '../../ui/TargetSelector'
import type { ParamsSchema } from '../cron/api'

/* Server vocabularies are typed `string`, not unions (as `pages/cron/api.ts`): a row from an older/newer API
 * renders as itself. */

export type ScheduleWhen = {
  kind: 'schedule'
  schedule_kind: string
  cron_expression: string | null
  interval_seconds: number | null
  retention_mode: string
  min_gap_seconds: number | null
  timeout_seconds: number | null
  /** The server's sentence ("Every day at 09:00 UTC"). */
  schedule_human: string
  /** The server's compact phrase for a regular schedule ("Daily 09:00 EDT", "Every 3h"); null when the expression
   *  has none, and the sentence stands in (`automation_service._when_out`). */
  schedule_short: string | null
}

export type EventWhen = {
  kind: 'event'
  source_kind: string
  source_config: Record<string, unknown>
  preflight_kind: string
  preflight_script_path: string | null
  preflight_prompt: string | null
  preflight_credential_id: string | null
  preflight_timeout_seconds: number
  concurrency_cap: number
  queue_depth: number
  rate_limit_per_hour: number
  budget_usd: number | null
  auto_pause_failures: number | null
  auto_pause_idle_days: number | null
  /** Where a sender posts; null when this control plane has no public hooks address (or no HTTP source). */
  ingress_url: string | null
  /** `none` (no signed source — an API-key webhook holds no secret), `owed` (not yet revealed), `revealed`. */
  secret_state: string
  health: { state: string; reason: string | null }
  state_document: Record<string, unknown>
}

export type When = ScheduleWhen | EventWhen

export type ActorRef = { id: string | null; name: string | null }

export type Automation = {
  id: string
  name: string
  slug: string
  description: string
  org_id: string
  owner: ActorRef
  is_system: boolean
  when: When
  target: TargetRule | null
  action: { type: string; params: Record<string, unknown> }
  paused: boolean
  /** `active` | `paused` | `failing`. */
  status: string
  status_reason: string | null
  /** The failure's short human cause ("Database unreachable", `automation_service.failure_cause`); the raw text
   *  stays in `status_reason`. Null unless `failing`. */
  status_cause: string | null
  /** A schedule whose firing outlived its timeout and still holds the run lock: offers Release. */
  stuck: boolean
  last_run_at: string | null
  /** The newest recorded run's outcome in the list's one vocabulary (`automation_service.LAST_RUN_OUTCOMES`:
   *  `filed` | `ran` | `noop` | `skipped` | `missed` | `error` | `pending`); null with no run on record. */
  last_run_outcome: string | null
  /** The newest gap of `missed` occurrences (a restart's backfill) as one fact: `count` rows, `more` when the
   *  backfill capped it. Null unless the newest recorded run is `missed`. */
  last_run_missed: { count: number; more: boolean } | null
  /** A schedule's newest recorded `acted` run: when it last did work, however many noops since. Null for an event
   *  and where no `acted` row survives. */
  last_acted_at: string | null
  next_run_at: string | null
  /** The cron or trigger row this automation sits over (old `/crons/:id`, `/triggers/:id` links, the state
   *  document's route). */
  legacy: { kind: 'cron' | 'trigger'; id: string }
  created_by_task_id: number | null
  created_at: string | null
  updated_at: string | null
  updated_by: ActorRef
}

/** What `POST` takes; a PATCH sends any subset. `target` is null iff the action takes none. */
export type AutomationInput = {
  name: string
  description: string
  org_id: string
  when: Record<string, unknown> & { kind: 'schedule' | 'event' }
  target: TargetRule | null
  action: { type: string; params: Record<string, unknown> }
  paused?: boolean
}

/** A create, or a When change to an event, answers its signed source's secret here — once. */
export type AutomationSaved = { automation: Automation; secret: string | null }

/** `GET /action-types`: the action types this caller may create (the registry's server half). */
export type ActionType = {
  type: string
  label: string
  description: string
  operator_only: boolean
  takes_target: boolean
  target_kinds: TargetKind[]
  when_kinds: Array<'schedule' | 'event'>
  timeout_seconds: number
  /** A system function's params form; null for `task`/`noop`. */
  params_schema: ParamsSchema | null
}

export type TargetPreviewWire = {
  resolved: Array<{ id: TargetId; name: string }>
  resolved_count: number
  universe_count: number
  dangling: TargetDangling[]
}

/** One target of one firing (`automation_run_targets`). */
export type RunTarget = {
  kind: string
  id: string
  name: string
  /** `filed` | `skipped` | `error` | `not_reached` | `queued`. */
  outcome: string
  reason: string
  task_id: number | null
}

/** One firing: a schedule's `cron_runs` row or an event's delivery. A run from before the fire engine has no
 *  targets and carries only its stored `detail`. */
export type AutomationRun = {
  id: string
  /** `schedule` | `event` | `manual`. */
  cause: string
  fired_at: string | null
  outcome: string
  outcome_label?: string
  detail: string
  /** A schedule skip's marker (`app/crons.py::SKIP_REASONS`); null or absent otherwise. */
  skip_reason?: string | null
  /** A failed run's short cause ("Database unreachable", `automation_service.failure_cause`); null otherwise. */
  failure_cause?: string | null
  task_id: number | null
  /** outcome -> reason -> count, over every target of the run. */
  target_counts: Record<string, Record<string, number>>
  targets_total: number
  /** The first `targets_limit` targets; `targets_total` says how many there are. */
  targets: RunTarget[]
  // Event runs only.
  preview?: Record<string, unknown> | null
  dedup?: { key: string; status: string } | null
  owed_targets?: number
  /** A received message's subject and sender (`From` as it arrived); null for any other delivery. */
  email?: { subject: string; from: string } | null
}

export type AutomationRunsPage = { items: AutomationRun[]; limit: number; offset: number }

const BASE = '/api/automations'
const one = (id: string) => `${BASE}/${encodeURIComponent(id)}`

/** The list's react-query key (`AutomationList.tsx`); the list re-reads on every visit. */
export const AUTOMATIONS_KEY = ['automations'] as const
export function listAutomations(filters: { when_kind?: string; status?: string } = {}) {
  const query = new URLSearchParams()
  if (filters.when_kind) query.set('when_kind', filters.when_kind)
  if (filters.status) query.set('status', filters.status)
  const qs = query.toString()
  return get<{ items: Automation[]; total: number }>(qs ? `${BASE}?${qs}` : BASE)
}
export const getAutomation = (id: string) => get<Automation>(one(id))
export const createAutomation = (input: AutomationInput) => post<AutomationSaved>(BASE, input)
export const updateAutomation = (id: string, input: Partial<AutomationInput>) => patch<AutomationSaved>(one(id), input)
export const deleteAutomation = (id: string) => del(one(id))
export const pauseAutomation = (id: string, reason = '') => post<Automation>(`${one(id)}/pause`, { reason })
export const resumeAutomation = (id: string) => post<Automation>(`${one(id)}/resume`)
/** End a stuck schedule's held run so the next tick runs; its run history stays. */
export const releaseAutomation = (id: string) => post<Automation & { released: boolean }>(`${one(id)}/release`)
export const runAutomation = (id: string, sample?: Record<string, unknown>) =>
  post<{ fired: boolean; detail: string; delivery_id?: number }>(`${one(id)}/run`, sample ? { sample } : undefined)
/** After Run now the firing is off the request: the run (and the list's last-run facts) land once it finishes, so a
 *  caller re-reads at these delays. One home for the detail page and the list. */
export const RUNS_REREAD_MS = [2500, 8000]
/** The runs list's `status` filter (`routes/automations.py::RUNS_STATUSES`); '' is every run. */
export type RunsFilter = { status?: string; q?: string }
export const listAutomationRuns = (
  id: string, offset = 0, limit = RUNS_PAGE_SIZE, targetsLimit = 50, filter: RunsFilter = {},
) => {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset), targets_limit: String(targetsLimit) })
  if (filter.status) query.set('status', filter.status)
  if (filter.q) query.set('q', filter.q)
  return get<AutomationRunsPage>(`${one(id)}/runs?${query.toString()}`)
}
/** One run by its `id` (the list item's), in the list item's shape — a pasted run URL's load. */
export const getAutomationRun = (automationId: string, runId: string, targetsLimit?: number) =>
  get<AutomationRun>(
    `${one(automationId)}/runs/${encodeURIComponent(runId)}${targetsLimit ? `?targets_limit=${targetsLimit}` : ''}`,
  )
/** One repository a project connects (`routes/projects.py::ProjectRepoOut`): `name` is its `owner/name`. */
export type ConnectedRepo = { name: string; forge: string | null; forge_base_url: string | null; is_primary: boolean }
/** The project's connected github.com repositories, primary first: the GitHub source's Repository choices (the
 *  server's `routes/triggers.py::_connected_github_repositories`). */
export const connectedGithubRepos = async (projectId: number): Promise<ConnectedRepo[]> => {
  const project = await get<{ repos?: ConnectedRepo[] }>(`/api/projects/${projectId}`)
  return (project.repos ?? []).filter(repo => repo.forge === 'github' && !repo.forge_base_url).sort((a, b) => Number(b.is_primary) - Number(a.is_primary))
}
export const listActionTypes = () => get<{ items: ActionType[]; total: number }>(`${BASE}/action-types`)
export const previewTarget = (orgId: string, target: TargetRule, automationId?: string | null) =>
  post<TargetPreviewWire>(`${BASE}/targets/preview`, {
    org_id: orgId,
    target,
    ...(automationId ? { automation_id: automationId } : {}),
  })
export const automationByLegacy = (kind: 'cron' | 'trigger', legacyId: string) =>
  get<{ id: string }>(`${BASE}/by-legacy/${kind}/${encodeURIComponent(legacyId)}`)

export function uploadAutomationAttachment(id: string, file: File) {
  const form = new FormData()
  form.append('file', file, file.name)
  return postForm<{ id: number; filename: string }>(`${one(id)}/attachments`, form)
}
export const deleteAutomationAttachment = (id: string, attachmentId: number) =>
  del(`${one(id)}/attachments/${attachmentId}`)

// Event-only: today's trigger behaviour on the automation's event child.
export const revealAutomationSecret = (id: string) => post<{ secret: string }>(`${one(id)}/reveal-secret`)
export const rotateAutomationSecret = (id: string) => post<{ secret: string }>(`${one(id)}/rotate-secret`)
export const testRenderAutomation = (
  id: string,
  sample: Record<string, unknown>,
  templates: { prompt_template?: string; title_template?: string } = {},
) => post<{ title: string; body: string; missing_paths: string[] }>(`${one(id)}/test-render`, { sample, ...templates })
export const listAutomationDedup = (id: string) =>
  get<{ items: Array<{ dedup_key: string; first_seen_at: string | null; task: { id: number; title: string | null } | null; delivery_id: number | null }>; total: number }>(`${one(id)}/dedup`)
export const clearAutomationDedup = (id: string, key: string) =>
  del(`${one(id)}/dedup/${key.split('/').map(encodeURIComponent).join('/')}`)
