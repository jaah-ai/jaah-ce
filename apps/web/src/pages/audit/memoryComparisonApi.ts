import { get } from '../../api'

/* The ON-vs-OFF comparison's wire shapes (the API's `ComparisonOut`). The server
 * decides every verdict — arm membership, `notice` (empty or under-n), the headline basis — and this page
 * renders them; it never computes a figure. */

export type Spread = { median: number; q1: number; q3: number }
export type UnitFigure = { total: number; median: number | null; q1: number | null; q3: number | null }
export type UnitKindKey = 'injected' | 'read' | 'cited' | 'followed'

export type ComparisonArm = {
  key: 'off' | 'on' | 'on_injected' | 'on_empty'
  label: string
  sessions: number
  tasks: number
  sources: Record<string, number>
  /** Set instead of figures: "No OFF tasks in this window", "Not enough OFF sessions in this window (n = 3)". */
  notice: string | null
  units: Record<UnitKindKey, UnitFigure> | null
  orientation: {
    calls_to_first_edit: Spread | null
    no_edit: number
    no_transcript: number
    duplicate_reads: Spread | null
  } | null
}

export type ReadRateRow = {
  kind: string
  injected: number
  used: number
  read: number
  cited: number
  read_rate: number | null
  written: number
  units_read: number
  write_to_read: number | null
}

export type MemoryComparison = {
  /** `off_by_plan`: the org's plan leaves project memory out; the arms are empty. Optional: older payloads. */
  state?: 'ok' | 'off_by_plan'
  project_id: number
  days: number
  since: string
  until: string
  persona: string | null
  min_n: number
  basis: 'holdout' | 'all'
  off: ComparisonArm
  on: ComparisonArm
  on_injected: ComparisonArm
  on_empty: ComparisonArm
  before_sessions: number
  mixed_tasks: number
  orientation_status: 'ok' | 'unavailable'
  orientation_reason: string | null
  read_rate: ReadRateRow[]
}

export type ComparisonProject = { id: number; name: string }

export const listComparisonProjects = () => get<ComparisonProject[]>('/api/memory/comparison/projects')

export function getMemoryComparison(projectId: number, days: number, persona: string): Promise<MemoryComparison> {
  const params = new URLSearchParams({ project_id: String(projectId), days: String(days) })
  if (persona) params.set('persona', persona)
  return get<MemoryComparison>(`/api/memory/comparison?${params.toString()}`)
}

/** At most one decimal, and no trailing `.0`: `2`, `0.5`, `12.3`. */
export function fmt(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

/** `2 units`, `1 call` — every figure carries its unit. */
export function withUnit(value: number, singular: string, plural = `${singular}s`): string {
  return `${fmt(value)} ${value === 1 ? singular : plural}`
}

export function percent(value: number | null): string | null {
  return value === null ? null : `${Math.round(value * 100)}%`
}
