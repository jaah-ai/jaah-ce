import { get } from '../api'

/* PIPELINE GATES AND DEPLOY TARGETS — the web half of the `gates` / `after` / `target` fields of
 * `GET`/`PUT /api/projects/{id}/pipeline` (`apps/api/app/pipeline_gates/schema.py`, the one home of the rules this file
 * checks before Save so a mistake shows beside its field; the server stays the authority). A gate's one-line `summary`
 * and its `warning` are the server's (AC1.8): this file never builds a summary. */

export type GateKind = 'command' | 'ci' | 'webhook' | 'probe' | 'alerts' | 'ticket'
export type TicketSystem = 'jira' | 'servicenow'
export type TicketState = { id: string; name: string }

/** A gate as read (`Gate`, architecture §3.3). */
export type Gate = {
  id: string
  name: string
  kind: GateKind
  enabled: boolean
  required: boolean
  timeout_s: number
  retries: number
  config: Record<string, unknown>
  credential: string | null
  summary: string
  warning: string | null
}
/** A gate as a save sends it: `id` null is a new one. */
export type GateIn = Omit<Gate, 'id' | 'summary' | 'warning'> & { id: string | null }
/** A gate in the editor: its draft `key`, and what the read said about it while unchanged. */
export type GateDraft = GateIn & { key: string; summary: string | null; warning: string | null }

export type TargetKind = 'none' | 'forge_env' | 'argocd' | 'webhook' | 'script'
export type Target = { kind: TargetKind; config: Record<string, unknown> | null; credential: string | null }

export const GATE_TIMEOUT_MIN = 60
export const GATE_TIMEOUT_MAX = 21600
export const GATE_RETRIES_MAX = 3
export const GATE_NAME_MAX = 80
export const GATES_MAX = 20
export const HTTPS_MESSAGE = 'Use an https:// address'
/** D8's exact words (AC7.3), as the server says them. */
export const ASK_ON_AUTO_MESSAGE = "An automatic step can't ask for a ticket. Choose a fixed ticket or query."
/** D7's sentence for `warning: "no_alert_source"`. */
export const NO_ALERT_SOURCE = 'No alert source connected — this gate always passes'

/** The seven choices of Add gate (AC1.2): the two ticket systems are one kind with `config.system`. */
export type GateChoice = 'command' | 'ci' | 'webhook' | 'probe' | 'alerts' | 'ticket_jira' | 'ticket_servicenow'
export const GATE_CHOICES: readonly { value: GateChoice; label: string; help: string }[] = [
  { value: 'command', label: 'Command on the node', help: "Runs in the project's checkout. Exit 0 passes." },
  { value: 'ci', label: 'CI workflow or pipeline', help: 'GitHub Actions, GitLab CI, Azure Pipelines, Bitbucket.' },
  { value: 'webhook', label: 'Webhook', help: 'Answers pass or fail with a short report.' },
  { value: 'probe', label: 'Health probe', help: "Checks this stage's address." },
  { value: 'alerts', label: 'Open alerts', help: 'Passes with no open incident.' },
  { value: 'ticket_jira', label: 'Ticket in Jira', help: 'Passes when a ticket is in a state you pick.' },
  { value: 'ticket_servicenow', label: 'Ticket in ServiceNow', help: 'Passes when a change request is in a state you pick.' },
]

/** Each kind's glyph and name, for a gate's row (never the raw kind). */
export const KIND_ICON: Record<GateKind, string> = { command: '⌘', ci: '⚙', webhook: '↗', probe: '♥', alerts: '🔔', ticket: '🎫' }
export const KIND_LABEL: Record<GateKind, string> = {
  command: 'Command', ci: 'CI run', webhook: 'Webhook', probe: 'Health probe', alerts: 'Open alerts', ticket: 'Ticket',
}

/** Incident severities (`production_qa/vocabulary.py::SEVERITIES`), lowest first. */
export const SEVERITIES: readonly { value: string; label: string }[] = [
  { value: 'debug', label: 'Debug' }, { value: 'info', label: 'Info' }, { value: 'warning', label: 'Warning' },
  { value: 'error', label: 'Error' }, { value: 'critical', label: 'Critical' },
]

export const TARGET_CHOICES: readonly { value: TargetKind; label: string; help: string }[] = [
  { value: 'none', label: 'None', help: 'Jaah deploys nothing; type the address.' },
  { value: 'forge_env', label: 'Forge deploy', help: 'The forge deploys on push to its environment; Jaah reads the deployment.' },
  { value: 'argocd', label: 'Argo CD app', help: 'Jaah syncs the app once a change lands.' },
  { value: 'webhook', label: 'Webhook', help: 'Jaah asks your service to deploy and waits for its answer.' },
  { value: 'script', label: 'Script', help: "Runs a command in the project's checkout once a change lands." },
]

/** The deploy target each driven source comes from: on a stage that deploys through another target (it changed
 *  since), the stored source no longer says where the address comes from, so the label names the current target's. */
const SOURCE_TARGET: Record<string, TargetKind> = { detected: 'forge_env', argocd: 'argocd', webhook: 'webhook', script: 'script' }

/** Where a stage's address came from, as its field says it. */
export function urlSourceLabel(source: string | null, target: TargetKind, forgeLabel: string | null): string {
  const owner = source === null ? undefined : SOURCE_TARGET[source]
  switch (owner !== undefined && target !== 'none' && owner !== target ? null : source) {
    case 'argocd': return 'From Argo CD'
    case 'webhook': return 'From webhook'
    case 'script': return 'From script'
    case 'detected': return forgeLabel ? `From ${forgeLabel} deployment` : 'From the forge deployment'
    case 'easy_mode': return 'From Easy Mode'
    case 'manual': return 'Typed'
    default:
      if (target === 'argocd') return 'From Argo CD once it syncs'
      if (target === 'webhook') return 'From webhook once it deploys'
      if (target === 'script') return 'From script once it runs'
      if (target === 'forge_env') return forgeLabel ? `From ${forgeLabel} deployment` : 'From the forge deployment'
      return 'Typed'
  }
}

let nextGateKey = 0
export const newGateKey = (): string => `gate-${++nextGateKey}`

/** A read gate as an editor row. */
export const gateDraftOf = (g: Gate): GateDraft => ({
  key: g.id, id: g.id, name: g.name, kind: g.kind, enabled: g.enabled, required: g.required, timeout_s: g.timeout_s,
  retries: g.retries, config: g.config ?? {}, credential: g.credential, summary: g.summary, warning: g.warning,
})

/** An editor row as the save sends it. */
export const gateIn = (g: GateDraft): GateIn => ({
  id: g.id, name: g.name.trim(), kind: g.kind, enabled: g.enabled, required: g.required, timeout_s: g.timeout_s,
  retries: g.retries, config: cleanConfig(g.kind, g.config), credential: g.credential?.trim() || null,
})

function cleanConfig(kind: GateKind, config: Record<string, unknown>): Record<string, unknown> {
  const text = (key: string) => (typeof config[key] === 'string' ? (config[key] as string).trim() : '')
  if (kind === 'ci') {
    return config.all_checks ? { mode: 'wait', workflow: null, all_checks: true }
      : { mode: config.mode === 'start' ? 'start' : 'wait', workflow: text('workflow') || null, all_checks: false }
  }
  if (kind === 'probe') {
    return { ...config, path: text('path') || '/', contains: text('contains') || null }
  }
  if (kind === 'alerts') return { min_severity: config.min_severity || null, environment: text('environment') || null }
  if (kind === 'ticket') return { ...config, query: config.source === 'ask' ? null : text('query') || null }
  if (kind === 'command') return { run: text('run') }
  if (kind === 'webhook') return { url: text('url') }
  return config
}

/** A new gate of the picked choice, with its kind's defaults (D3: the command gate keeps the merge lane's). */
export function newGate(choice: GateChoice, autoStep: boolean): GateDraft {
  const base = { key: newGateKey(), id: null, enabled: true, required: true, credential: null, summary: null, warning: null }
  switch (choice) {
    case 'command': return { ...base, kind: 'command', name: 'Command', timeout_s: 5400, retries: 1, config: { run: '' } }
    case 'ci': return { ...base, kind: 'ci', name: 'CI run', timeout_s: 3600, retries: 0, config: { mode: 'start', workflow: '', all_checks: false } }
    case 'webhook': return { ...base, kind: 'webhook', name: 'Webhook', timeout_s: 600, retries: 0, config: { url: '' } }
    case 'probe': return { ...base, kind: 'probe', name: 'Health check', timeout_s: 300, retries: 0,
      config: { path: '/health', expect_min: 200, expect_max: 399, contains: null, tries: 3 } }
    case 'alerts': return { ...base, kind: 'alerts', name: 'No open alerts', timeout_s: 60, retries: 0,
      config: { min_severity: 'error', environment: null } }
    case 'ticket_jira':
    case 'ticket_servicenow': return { ...base, kind: 'ticket', name: choice === 'ticket_jira' ? 'Jira ticket' : 'ServiceNow ticket',
      timeout_s: 3600, retries: 0,
      config: { system: choice === 'ticket_jira' ? 'jira' : 'servicenow', source: autoStep ? 'fixed' : 'ask', query: '', states: [] } }
  }
}

/** Whether `url` is an https address (the server's `_https`). */
function isHttps(url: string): boolean {
  try {
    const parsed = new URL(url.trim())
    return parsed.protocol === 'https:' && parsed.host !== ''
  } catch {
    return false
  }
}

/** What the save would refuse in one gate, by field — so it shows beside the field before Save (AC1.3, AC4.3, AC7.3). */
export function gateProblems(g: GateDraft, autoStep: boolean): Record<string, string> {
  const out: Record<string, string> = {}
  const name = g.name.trim()
  if (!name) out.name = 'Name it.'
  else if (name.length > GATE_NAME_MAX) out.name = `At most ${GATE_NAME_MAX} characters.`
  if (!Number.isInteger(g.timeout_s) || g.timeout_s < GATE_TIMEOUT_MIN || g.timeout_s > GATE_TIMEOUT_MAX) {
    out.timeout_s = 'Between 1 minute and 6 hours.'
  }
  if (!Number.isInteger(g.retries) || g.retries < 0 || g.retries > GATE_RETRIES_MAX) out.retries = `0 to ${GATE_RETRIES_MAX}.`
  const c = g.config
  const text = (key: string) => (typeof c[key] === 'string' ? (c[key] as string).trim() : '')
  if (g.kind === 'command' && !text('run')) out.run = 'Type a command.'
  if (g.kind === 'ci' && !c.all_checks && !text('workflow')) out.workflow = 'Pick or type a workflow.'
  if (g.kind === 'webhook') {
    if (!text('url')) out.url = 'Type its address.'
    else if (!isHttps(text('url'))) out.url = HTTPS_MESSAGE
    if (!g.credential?.trim()) out.credential = 'A webhook needs its signing secret.'
  }
  if (g.kind === 'probe') {
    const path = text('path') || '/'
    if (!path.startsWith('/')) out.path = 'Start with /.'
    const min = Number(c.expect_min)
    const max = Number(c.expect_max)
    if (!Number.isInteger(min) || !Number.isInteger(max) || min < 100 || max > 599 || min > max) {
      out.expect = 'A range like 200 to 399.'
    }
    const tries = Number(c.tries)
    if (!Number.isInteger(tries) || tries < 1 || tries > 10) out.tries = '1 to 10.'
  }
  if (g.kind === 'ticket') {
    if (c.source === 'ask' && autoStep) out.source = ASK_ON_AUTO_MESSAGE
    if (c.source !== 'ask' && !text('query')) out.query = 'Type a ticket or query.'
    if (!Array.isArray(c.states) || c.states.length === 0) out.states = 'Pick at least one state.'
    if (!g.credential?.trim()) out.credential = 'Pick the login it reads tickets with.'
    else if (/^me\//i.test(g.credential.trim())) out.credential = 'Pick a credential shared with this project, not a personal one.'
  }
  if (g.credential && g.credential.trim() && !/^(org|projects|me)\//.test(g.credential.trim())) {
    out.credential = 'A saved credential, like org/qa-hook.'
  }
  return out
}

/** What the save would refuse in a stage's deploy target, by field. */
export function targetProblems(t: Target): Record<string, string> {
  const out: Record<string, string> = {}
  const c = t.config ?? {}
  const text = (key: string) => (typeof c[key] === 'string' ? (c[key] as string).trim() : '')
  if (t.kind === 'argocd') {
    if (!text('server')) out.server = 'Type the server address.'
    else if (!isHttps(text('server'))) out.server = HTTPS_MESSAGE
    if (!text('app')) out.app = 'Name the Argo CD app.'
  }
  if (t.kind === 'webhook') {
    if (!text('url')) out.url = 'Type its address.'
    else if (!isHttps(text('url'))) out.url = HTTPS_MESSAGE
    if (!t.credential?.trim()) out.credential = 'A webhook needs its signing secret.'
  }
  if (t.kind === 'script' && !text('run')) out.run = 'Type a command.'
  if (t.credential && t.credential.trim() && !/^(org|projects|me)\//.test(t.credential.trim())) {
    out.credential = 'A saved credential, like org/argocd.'
  }
  return out
}

/** A target as the save sends it: only its own kind's fields. */
export function targetIn(t: Target): Target {
  const c = t.config ?? {}
  const text = (key: string) => (typeof c[key] === 'string' ? (c[key] as string).trim() : '')
  const credential = t.credential?.trim() || null
  switch (t.kind) {
    case 'argocd': return { kind: 'argocd', config: { server: text('server'), app: text('app'), app_namespace: text('app_namespace') || null }, credential }
    case 'webhook': return { kind: 'webhook', config: { url: text('url') }, credential }
    case 'script': return { kind: 'script', config: { run: text('run') }, credential }
    default: return { kind: t.kind, config: null, credential: null }
  }
}

/** "90 min", "6 h", "1 h 30 min". */
export function durationText(seconds: number): string {
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

/* The lazy pickers (§3.3). Each answers `[]` when the list can't be read, so the field falls back as it says. */
export type CiWorkflow = { id: string; name: string; path: string }
export const ciWorkflowsKey = (projectId: number) => ['pipeline-ci-workflows', projectId] as const
export const getCiWorkflows = (projectId: number) => get<CiWorkflow[]>(`/api/projects/${projectId}/pipeline/ci-workflows`)

export const ticketStatesKey = (projectId: number, system: TicketSystem, credential: string | null) =>
  ['pipeline-ticket-states', projectId, system, credential] as const
/** A ticket system's states (`[{id, name}]`) read with `credential`, or null before a login is picked. A login that
 *  can't be used, or a system that can't be read, throws the server's sentence (an `ApiError`). */
export async function getTicketStates(projectId: number, system: TicketSystem, credential: string | null): Promise<TicketState[] | null> {
  if (!credential) return null
  const query = new URLSearchParams({ system, credential })
  return await get<TicketState[]>(`/api/projects/${projectId}/pipeline/ticket-states?${query.toString()}`)
}

/** What a typed ticket names, for the step into `stageId` that asks for one (the Deploy dialog's check). */
export type TicketLookup = { found: boolean; key: string; status: string | null; url: string | null }
export const lookupTicket = (projectId: number, stageId: string, key: string) =>
  get<TicketLookup>(`/api/projects/${projectId}/pipeline/ticket?${new URLSearchParams({ stage_id: stageId, key }).toString()}`)

/* GATE RESULTS (D15): each run of a gate list and a row per gate, read from `GET /api/projects/{id}/gate-runs`
 * (`apps/api/app/pipeline_gates/results.py::runs_out`). Reports are plain text, shown as text. */
export type GateResultState = 'waiting' | 'running' | 'passed' | 'failed' | 'timed_out' | 'off' | 'skipped'
export type GateResultRow = {
  position: number
  gate_id: string | null
  name: string | null
  kind: GateKind | null
  required: boolean | null
  summary: string | null
  state: GateResultState
  attempts: number
  started_at: string | null
  finished_at: string | null
  report: string | null
  link: string | null
}
export type GateRunOut = {
  run: {
    id: string; stage_id: string | null; phase: 'before' | 'after'; subject_kind: 'lane_batch' | 'release' | 'arrival'
    subject_id: string; sha: string | null; state: 'running' | 'passed' | 'failed' | 'canceled'
    created_at: string | null; finished_at: string | null
  }
  results: GateResultRow[]
}
export type GateRunsQuery = { subjectKind: 'lane_batch' | 'release' | 'arrival'; subjectId?: string; stageId?: string }
  | { taskId: number }

export const gateRunsKey = (projectId: number, q: GateRunsQuery) => ['gate-runs', projectId, q] as const
export function getGateRuns(projectId: number, q: GateRunsQuery): Promise<{ runs: GateRunOut[] }> {
  const query = new URLSearchParams('taskId' in q
    ? { task_id: String(q.taskId) }
    : { subject_kind: q.subjectKind, ...(q.subjectId ? { subject_id: q.subjectId } : {}), ...(q.stageId ? { stage_id: q.stageId } : {}) })
  return get<{ runs: GateRunOut[] }>(`/api/projects/${projectId}/gate-runs?${query.toString()}`)
}

/** The words a gate's result reads as (AC1.4–AC1.6): "Passed on attempt 3", "Failed after 2 attempts", amber
 *  "Failed (advisory)". */
export function gateResultWords(r: Pick<GateResultRow, 'state' | 'attempts' | 'required'>): string {
  const tries = r.attempts > 1
  switch (r.state) {
    case 'passed': return tries ? `Passed on attempt ${r.attempts}` : 'Passed'
    case 'failed':
      if (r.required === false) return tries ? `Failed (advisory) after ${r.attempts} attempts` : 'Failed (advisory)'
      return tries ? `Failed after ${r.attempts} attempts` : 'Failed'
    case 'timed_out':
      if (r.required === false) return 'Timed out (advisory)'
      return tries ? `Timed out after ${r.attempts} attempts` : 'Timed out'
    case 'off': return 'Off'
    case 'skipped': return 'Skipped'
    case 'running': return 'Running'
    default: return 'Waiting'
  }
}

/** How long a gate took, once it started: "12 s", "41 min". */
export function gateDuration(r: Pick<GateResultRow, 'started_at' | 'finished_at'>, now = Date.now()): string | null {
  if (!r.started_at) return null
  const end = r.finished_at ? Date.parse(r.finished_at) : now
  const seconds = Math.max(0, Math.round((end - Date.parse(r.started_at)) / 1000))
  if (!Number.isFinite(seconds)) return null
  return seconds < 60 ? `${seconds} s` : durationText(seconds)
}
