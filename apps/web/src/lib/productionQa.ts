import { get, patch, post, withQuery } from '../api'
import { presentationOf, type Presentation } from '../ui/statusTone'
import { shortSha } from './shortSha'

/* Production QA's read side for the console: the shapes `GET /api/deployments[/{id}]` and `GET /api/incidents[/{id}]`
 * answer (`apps/api/app/routes/production_qa.py`), the fetchers, and the vocabulary's words and tones. The
 * vocabulary's tokens are the server's (`apps/api/app/production_qa/vocabulary.py`); this file only words them.
 * Pages: `pages/production-qa/`. */

export type ProjectRef = { id: number; name: string }

export type Deployment = {
  id: number
  project: ProjectRef
  repo_path: string
  forge: string
  workflow_path: string
  workflow_name: string | null
  run_id: number
  run_attempt: number
  run_event: string | null
  run_url: string | null
  head_branch: string | null
  head_sha: string
  environment: string
  is_production: boolean
  shipped: boolean
  is_anchor: boolean
  status: DeploymentStatus
  conclusion_raw: string | null
  failing_job: string | null
  failing_step: string | null
  range_from_sha: string | null
  range_status: RangeStatus | null
  range_note: string | null
  started_at: string | null
  finished_at: string | null
  first_received_at: string
  last_received_at: string
  regression_due_at: string | null
  regression_status: 'pending' | 'measured' | 'not_measured' | null
  regression_note: string | null
  pr_numbers: number[]
}

export type DeploymentEnvironment = { environment: string; job_name: string | null; state: string; received_at: string }
export type DeploymentPr = { repo_path: string; pr_number: number; task_id: number | null; merge_sha: string; source: string }
export type DeploymentMetric = {
  id: number
  metric: string
  trigger_id: string | null
  baseline: number | null
  post: number | null
  delta_abs: number | null
  delta_pct: number | null
  events_post: number | null
  window_min: number
  breached: boolean
  note: string | null
  measured_at: string
}
export type LedgerEdge = {
  id: number
  task_id: number
  kind: string
  deployment_id: number | null
  incident_id: number | null
  occurred_at: string
  revoked_at: string | null
  applied_at: string | null
}
export type Probe = {
  incident_id: number
  title: string
  state: IncidentState
  environment: string | null
  last_seen: string
  facts: Record<string, unknown>
}

/** The `deploy_probe` run filed for a deploy; `reported_at` null until its runner reports. */
export type ProbeRun = { task_id: number; state: string; reported_at: string | null; skipped?: string | null }
/** One probe's result from that run, passes too; `incident_id` set when a failure filed one. */
export type ProbeResult = {
  probe_id: string
  kind: 'http' | 'command'
  name: string
  ok: boolean
  status: number | null
  exit_code: number | null
  duration_s: number | null
  output_tail: string
  incident_id: number | null
}

export type DeploymentDetail = Deployment & {
  environments: DeploymentEnvironment[]
  commits: string[]
  prs: DeploymentPr[]
  metrics: DeploymentMetric[]
  edges: LedgerEdge[]
  /** The `health_breach` incidents on this deploy. */
  probes: Probe[]
  probe_run: ProbeRun | null
  probe_results: ProbeResult[]
}

export type TopSuspect = { repo_path: string; pr_number: number | null; task_id: number | null; confidence: Confidence; state: string }

export type Incident = {
  id: number
  project: ProjectRef
  type: IncidentType
  source_kind: string
  trigger_id: string | null
  fingerprint: string
  title: string
  severity: Severity
  environment: string | null
  state: IncidentState
  first_seen: string
  last_seen: string
  last_transition_at: string
  count: number
  count_mode: string
  release: string | null
  commit_sha: string | null
  vendor_url: string | null
  vendor_ref: string | null
  deployment_id: number | null
  resolved_at: string | null
  created_at: string
  top_suspect: TopSuspect | null
}

export type Suspect = {
  id: number
  deployment_id: number | null
  task_id: number | null
  repo_path: string
  pr_number: number | null
  basis: string
  confidence: Confidence
  score: number
  state: string
  decided_by: string | null
  /** Who decided, as the console names a person; null for a computed suggestion. */
  decided_by_name?: string | null
  decided_at: string | null
  created_at: string
}
export type IncidentEvent = { id: number; kind: string; at: string; delivery_id: number | null; actor_id: string | null; note: string | null }
export type IncidentTask = { task_id: number | null; role: string; delivery_id: number | null; claimed_at: string; released_at: string | null }
export type StackFrame = { path: string | null; function: string | null; lineno: number | null; in_app: boolean | null }

export type IncidentDetail = Incident & {
  stack_frames: StackFrame[] | null
  facts: Record<string, unknown>
  suspects: Suspect[]
  events: IncidentEvent[]
  tasks: IncidentTask[]
  edges: LedgerEdge[]
  metrics: DeploymentMetric[]
  environments: DeploymentEnvironment[]
  /** May the viewer correct the suspects (`projects:edit` on the incident's project)? */
  can_edit?: boolean
  /** Why the project's revert policy filed no revert ("Revert: no automation"), else null. */
  revert_note?: string | null
}

export type Page<T> = { items: T[]; next_cursor: string | null }

/* ── Vocabulary: token → word, tone, glyph ─────────────────────────────────────────────────────────── */

export const DEPLOYMENT_STATUSES = ['queued', 'in_progress', 'success', 'failure', 'cancelled', 'skipped'] as const
export type DeploymentStatus = (typeof DEPLOYMENT_STATUSES)[number]
export type RangeStatus = 'first' | 'resolved' | 'partial' | 'pending' | 'rollback'
export const INCIDENT_TYPES = ['monitor', 'deploy_failed', 'health_breach', 'regression', 'lane_slowdown'] as const
export type IncidentType = (typeof INCIDENT_TYPES)[number]
/* Highest first, as a reader scans them (the server lists lowest first). */
export const SEVERITIES = ['critical', 'error', 'warning', 'info', 'debug'] as const
export type Severity = (typeof SEVERITIES)[number]
export const INCIDENT_STATES = ['open', 'reopened', 'resolved'] as const
export type IncidentState = (typeof INCIDENT_STATES)[number]
export type Confidence = 'high' | 'medium' | 'low'

type Word = Presentation

export const DEPLOYMENT_STATUS: Record<DeploymentStatus, Word> = {
  queued: { label: 'Queued', tone: 'neutral', glyph: '○' },
  in_progress: { label: 'Releasing', tone: 'info', glyph: '»' },
  success: { label: 'Succeeded', tone: 'success', glyph: '✓' },
  failure: { label: 'Failed', tone: 'error', glyph: '✕' },
  cancelled: { label: 'Cancelled', tone: 'neutral', glyph: '■' },
  skipped: { label: 'Skipped', tone: 'neutral', glyph: '–' },
}

export const SEVERITY: Record<Severity, Word> = {
  critical: { label: 'Critical', tone: 'error', glyph: '!' },
  error: { label: 'Error', tone: 'error', glyph: '✕' },
  warning: { label: 'Warning', tone: 'warning', glyph: '▲' },
  info: { label: 'Info', tone: 'info', glyph: 'i' },
  debug: { label: 'Debug', tone: 'neutral', glyph: '·' },
}

export const INCIDENT_STATE: Record<IncidentState, Word> = {
  open: { label: 'Open', tone: 'error', glyph: '●' },
  reopened: { label: 'Reopened', tone: 'warning', glyph: '↻' },
  resolved: { label: 'Resolved', tone: 'success', glyph: '✓' },
}

export const INCIDENT_TYPE: Record<IncidentType, string> = {
  monitor: 'Monitor',
  deploy_failed: 'Release failed',
  health_breach: 'Health',
  regression: 'Regression',
  lane_slowdown: 'Lane slowdown',
}

export const CONFIDENCE: Record<Confidence, Word> = {
  high: { label: 'High', tone: 'error', glyph: '' },
  medium: { label: 'Medium', tone: 'warning', glyph: '' },
  low: { label: 'Low', tone: 'neutral', glyph: '' },
}

/** A word for a token the console has no word for yet (a vocabulary widened server-side): itself, neutral. */
export function wordOf<K extends string>(table: Record<K, Word>, token: string): Word {
  return presentationOf(table, token, { label: token, tone: 'neutral', glyph: '?' })
}

export const METRIC_LABEL: Record<string, string> = { error_rate: 'Error rate', p95_latency_ms: 'p95 latency' }

/** What a vendor did not send: shown in words, never blank and never a made-up value (AC-9). */
export const NOT_SENT = 'Not sent'

/** The commit range a deploy shipped, in words: `a1b2c3d..e4f5a6b`, or what the range is when there is none. */
export function rangeText(d: Pick<Deployment, 'range_status' | 'range_from_sha' | 'head_sha'>): string {
  switch (d.range_status) {
    case 'first': return 'First recorded release'
    case 'rollback': return 'Rollback'
    case 'pending': return 'Pending'
    case 'partial': return d.range_from_sha ? `Partial · ${shortSha(d.range_from_sha)}..${shortSha(d.head_sha) ?? ''}` : 'Partial'
    case 'resolved': return d.range_from_sha ? `${shortSha(d.range_from_sha)}..${shortSha(d.head_sha) ?? ''}` : shortSha(d.head_sha) ?? ''
    default: return shortSha(d.head_sha) ?? ''
  }
}

/** A PR's short name: `#12`, with its repository when the deploy spans more than one. */
export function prName(pr: { repo_path: string; pr_number: number | null }, withRepo: boolean): string {
  if (pr.pr_number === null) return pr.repo_path
  return withRepo ? `${pr.repo_path}#${pr.pr_number}` : `#${pr.pr_number}`
}

function fixed(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(Math.abs(value) < 1 ? 4 : 2).replace(/0+$/, '').replace(/\.$/, '')
}

/** A metric value in its unit: an error rate as a percentage, a latency in ms. */
export function metricValue(metric: string, value: number | null): string {
  if (value === null) return '—'
  if (metric === 'error_rate') return `${fixed(value * 100)}%`
  if (metric === 'p95_latency_ms') return `${fixed(value)} ms`
  return fixed(value)
}

/** The change, absolute (in the metric's unit) and relative: `+4% (+400%)`. */
export function metricDelta(m: Pick<DeploymentMetric, 'metric' | 'delta_abs' | 'delta_pct'>): string {
  const sign = (v: number) => (v > 0 ? '+' : '')
  const abs = m.delta_abs === null ? null : `${sign(m.delta_abs)}${metricValue(m.metric, m.delta_abs)}`
  const pct = m.delta_pct === null ? null : `${sign(m.delta_pct)}${fixed(m.delta_pct)}%`
  if (abs && pct) return `${abs} (${pct})`
  return abs ?? pct ?? '—'
}

/* ── Reads ─────────────────────────────────────────────────────────────────────────────────────────── */

/** Lists poll at this period while the page is visible (react-query skips a hidden tab). */
export const PRODUCTION_QA_POLL_MS = 15_000
/** One read's worth of rows (the API's cap): the lists show the newest this many under the filters. */
export const PRODUCTION_QA_PAGE = 200

export const listDeployments = (filters: { project_id: string; environment: string; status: string }) =>
  get<Page<Deployment>>(withQuery('/api/deployments', { ...filters, limit: PRODUCTION_QA_PAGE }))
export const getDeployment = (id: string) => get<DeploymentDetail>(`/api/deployments/${encodeURIComponent(id)}`)
export const listIncidents = (filters: { project_id: string; severity: string; state: string; type: string }) =>
  get<Page<Incident>>(withQuery('/api/incidents', { ...filters, limit: PRODUCTION_QA_PAGE }))
/** The incidents a task was filed for (the task page's "Incident" chip). */
export const incidentsForTask = (taskId: number) =>
  get<Page<Incident>>(`/api/incidents?task_id=${taskId}&limit=1`)
export const getIncident = (id: string) => get<IncidentDetail>(`/api/incidents/${encodeURIComponent(id)}`)

/* ── Suspect corrections (`projects:edit`; each answers the incident as its detail read does) ──────── */

export type SuspectDecision = 'confirmed' | 'dismissed' | 'suggested'
export const decideSuspect = (incidentId: number, suspectId: number, state: SuspectDecision) =>
  patch<IncidentDetail>(`/api/incidents/${incidentId}/suspects/${suspectId}`, { state })
export const addSuspect = (incidentId: number, repoPath: string, prNumber: number) =>
  post<IncidentDetail>(`/api/incidents/${incidentId}/suspects`, { repo_path: repoPath, pr_number: prNumber })
