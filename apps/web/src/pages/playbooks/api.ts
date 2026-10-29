import { del, get, post, put } from '../../api'
import type { OrgRef } from '../../types'
import { humanize } from '../../lib/humanize'

/* THE PLAYBOOKS WIRE — the types and calls of `/api/playbooks` (`apps/api/app/routes/playbooks.py` is the
 * contract's single home; these types mirror it). The task form's Depth control reads `listPlaybooks` to offer
 * the org's own (`tenant`) playbooks while the org's plan has Playbooks on (`components/tasks/TaskAxisControls.tsx`). */

export type ExpertSpec =
  | { kind: 'pinned'; expert: string }
  | { kind: 'chosen_at_start'; default: string; allowed: string[] }
export type CreatedSpec = { kind: 'at_start' } | { kind: 'by'; stage: string }
export type DependsSpec = { kind: 'none' } | { kind: 'all_earlier' } | { kind: 'stages'; keys: string[] }

export type StageIn = {
  key: string
  label: string
  expert: ExpertSpec
  /** Null iff the expert is chosen at start: the deliverable then follows the expert. */
  deliverable: string | null
  seniority: string
  created: CreatedSpec
  runs: string
  depends_on: DependsSpec
  interviews: string
  extra_instructions: string
  /** Null = the deliverable's whole filing set; a list only narrows it. */
  may_file: string[] | null
}

export type ExpertFacts = { mode: string; ends_when: string; hands_off: string[] }

export type Derived = {
  tier: string | null
  mode: string | null
  ends_when: string | null
  deliverable_by_expert?: Record<string, string>
  by_expert?: Record<string, ExpertFacts>
  receives: string[]
  hands_off: string[]
  may_file_options: string[]
}

export type StageOut = StageIn & { derived: Derived }

export type Issue = {
  stage: string | null
  /** Authoritative for placing a message on a card; null = a playbook-level issue. */
  stage_index: number | null
  field: string | null
  severity: 'error' | 'warning'
  message: string
}

export type PlaybookIn = {
  name: string
  description: string
  entry: string
  full_auto_default: boolean
  default_seniority: string
  stages: StageIn[]
}

export type PlaybookOut = Omit<PlaybookIn, 'stages'> & {
  id: string
  origin: 'built_in' | 'tenant'
  /** A tenant copy's org; `null` for a built-in (one global copy). An operator's `all_orgs` list carries
   *  every org's copies, another org's read-only (`editable` and `may_duplicate` false). */
  org: OrgRef | null
  editable: boolean
  /* The caller may duplicate it — a built-in too, which is never `editable` (`routes/playbooks.py::_out`). */
  may_duplicate: boolean
  stages: StageOut[]
  issues: Issue[]
  /** May a task run it now? False with `not_runnable_reason` saying why (the task form greys it). */
  runnable: boolean
  not_runnable_reason: string | null
  /** One stage: the task runs as that stage's persona (`default_persona`, overridable). Else a staged run. */
  simple: boolean
  default_persona: string | null
  created_at: string | null
  updated_at: string | null
}

export type Vocabulary = {
  experts: { key: string; label: string }[]
  deliverables: {
    key: string
    label: string
    mode: string
    ends_when: string
    may_file: { key: string; label: string }[]
  }[]
  seniorities: { key: string; label: string; tier: string }[]
  labels: Record<string, Record<string, string>>
}

export type ValidateOut = { issues: Issue[]; stages: StageOut[] }
export type PromptOut = { system_prompt: string; task_prompt: string; model_tier: string }

const BASE = '/api/playbooks'
const one = (id: string) => `${BASE}/${encodeURIComponent(id)}`

export const PLAYBOOKS_KEY = ['playbooks'] as const
export const playbookKey = (id: string) => ['playbooks', 'one', id] as const
export const VOCABULARY_KEY = ['playbooks', 'vocabulary'] as const

export const listPlaybooks = () => get<{ playbooks: PlaybookOut[] }>(BASE).then(r => r.playbooks)
/** The Playbooks list's read: for an operator EVERY org's copies (`?all_orgs=true`), for anyone else the
 *  same answer as `listPlaybooks` (the server ignores the flag). Its own key, under `PLAYBOOKS_KEY` so every
 *  invalidation of that prefix refreshes it; the task form's picker keeps the caller org's own list. */
export const ALL_ORGS_PLAYBOOKS_KEY = ['playbooks', 'all-orgs'] as const
export const listAllOrgsPlaybooks = () =>
  get<{ playbooks: PlaybookOut[] }>(`${BASE}?all_orgs=true`).then(r => r.playbooks)
export const getPlaybook = (id: string) => get<PlaybookOut>(one(id))
export const getVocabulary = () => get<Vocabulary>(`${BASE}/vocabulary`)
export const createPlaybook = (body: PlaybookIn) => post<PlaybookOut>(BASE, body)
export const updatePlaybook = (id: string, body: PlaybookIn) => put<PlaybookOut>(one(id), body)
export const duplicatePlaybook = (id: string) => post<PlaybookOut>(`${one(id)}/duplicate`)
export const deletePlaybook = (id: string) => del(one(id))
export const validatePlaybook = (body: PlaybookIn) => post<ValidateOut>(`${BASE}/validate`, body)
export const previewPrompt = (body: { playbook: PlaybookIn; stage_key: string; expert?: string; full_auto?: boolean }) =>
  post<PromptOut>(`${BASE}/preview-prompt`, body)

/** The `PlaybookIn` a saved playbook round-trips as — `derived` dropped, every other key kept. */
export function inputOf(pb: PlaybookOut): PlaybookIn {
  return {
    name: pb.name,
    description: pb.description,
    entry: pb.entry,
    full_auto_default: pb.full_auto_default,
    default_seniority: pb.default_seniority,
    stages: pb.stages.map(({ derived: _derived, ...stage }) => stage),
  }
}

/** The issues a 422 carries (`{"detail": {"issues": [...]}}`), or null for any other refusal. */
export function issuesOf(body: unknown): Issue[] | null {
  const detail = (body as { detail?: { issues?: unknown } } | null)?.detail
  return detail && Array.isArray(detail.issues) ? (detail.issues as Issue[]) : null
}

/** A vocabulary value's human label. The server labels every closed set, so the fallback only guards a
 *  token the console has not yet learned — spelled as words, never as the raw token. */
export function labelOf(vocab: Vocabulary | undefined, field: string, key: string | null | undefined): string {
  if (!key) return ''
  const hit = vocab?.labels[field]?.[key]
  if (hit) return hit
  if (field === 'expert') return vocab?.experts.find(e => e.key === key)?.label ?? humanize(key)
  if (field === 'deliverable') return vocab?.deliverables.find(d => d.key === key)?.label ?? humanize(key)
  if (field === 'tier') return TIER_LABELS[key] ?? humanize(key)
  return humanize(key)
}

/* The model tier a seniority resolves to, in words (`SENIORITY_TIERS`: junior=low, mid=standard,
 * senior=high). The vocabulary serves the tier token but no label for it. */
const TIER_LABELS: Record<string, string> = { low: 'Low', standard: 'Standard', high: 'High' }
