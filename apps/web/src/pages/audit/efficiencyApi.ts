/* RUNNER EFFICIENCY OVER A PERIOD — the Sessions tab's second leaf, and its own wire client.
 *
 * ONE READ: `GET /api/sessions/efficiency?since=&until=`, `system:view`. An aggregate over the session
 * transcripts in object storage (Athena), so it can be honestly UNAVAILABLE where that storage is not
 * configured. Page-local wire module, not `src/types.ts` (one consumer).
 *
 * THIS FILE COMPUTES NOTHING. Every share, median, percentile and dollar figure is the server's, from one
 * query over one window, so the parts agree with the whole. A browser-side re-derivation would be a
 * second, quieter answer.
 */

import { get } from '../../api'

/* THE PHASE VOCABULARY — CLOSED, ORDERED, NEVER SORTED BY SIZE: a small phase is itself a finding, and
 * the stacked bar reads left to right as a run does.
 *
 * `awaiting_human` IS IN THE VOCABULARY AND OUT OF THE ARITHMETIC: it is the operator's response time,
 * so the server excludes it from every `share` denominator and `rework_share`. Never draw it in the split,
 * and never let its absence pass unexplained. */
export const PHASE_ORDER = [
  'exploration',
  'analysis',
  'coding',
  'verification',
  'gate',
  'integration',
  'report',
  'settle',
  'waiting',
  'awaiting_human',
  'other',
] as const

/** Where a phase sorts. An unknown phase (a control plane ahead of this bundle) sorts LAST rather than
 *  being dropped, so the parts still sum to the whole. Stable, since `sort` is stable. */
export function phaseRank(phase: string): number {
  const index = (PHASE_ORDER as readonly string[]).indexOf(phase)
  return index === -1 ? PHASE_ORDER.length : index
}

/** False for `awaiting_human` alone. One home for the exclusion. */
export function isAttributed(phase: string): boolean {
  return phase !== 'awaiting_human'
}

/* ── The response ──────────────────────────────────────────────────────────────────────────────── */

/** One phase's whole account of itself.
 *
 *  `phase` IS AN OPEN STRING: the wire can carry a value this bundle does not know; it renders as itself.
 *  `share` is of the ATTRIBUTED total (`awaiting_human`'s own share is not comparable; printed "excluded").
 *  `median_seconds` is over the runs that HAVE this phase, not all runs.
 *  `cost_usd` is a DECIMAL STRING or null; null means unpriced, not zero. */
export type PhaseStat = {
  phase: string
  seconds: number
  share: number
  median_seconds: number
  calls: number
  sessions: number
  tokens_in: number
  tokens_out: number
  cache_read: number
  cache_write: number
  cost_usd: string | null
  /** Non-empty for `waiting` alone. Absent from an older control plane, so readers default it. */
  wait_kinds?: WaitKindStat[]
}

/** WHAT ONE `waiting` SPAN WAS WAITING ON (gate, subagent, test — different fixes). `share` is of the
 *  WAITING PHASE, so these sum to 1.0. A kind that measured nothing is not a row. */
export type WaitKindStat = {
  kind: string
  seconds: number
  share: number
}

/** `exploration` AFTER THE CORRECTION, PER SESSION. On the wire, not divided here: the denominator is the
 *  sessions the PHASE query saw, while the envelope's `runs` is a wider set and would print a smaller
 *  number than the bar supports. `minutes_per_session` is NULL with no denominator, never `0.0`. */
export type GenuineExploration = {
  seconds: number
  sessions: number
  minutes_per_session: number | null
}

/** Per-model spend over the window (the "Per model" card). NO `seconds` FIELD: several models spend
 *  tokens inside one phase segment, so a per-model second would be double-counted or invented. Tokens and
 *  money are additive across models. */
export type ModelStat = {
  model: string
  tokens_in: number
  tokens_out: number
  cache_read: number
  cache_write: number
  cost_usd: string | null
}

/** GATE CHURN — how many times a session ran the project's FULL gate before it passed. Median AND p90
 *  (a median of 1 with a p90 of 9 hides sessions burning hours), plus a headcount over one run.
 *
 *  `rework_seconds`: wall-clock in `coding` AFTER the first full-gate call; `rework_share` uses the
 *  phase-share denominator. `sessions_over_one_run_share` is the server's division, NULL when
 *  `sessions_with_full_gate` is 0.
 *
 *  `full_gate_runs_total` counts every run, `gate_failures_total` the RED ones; `red_green_cycles_total`
 *  counts a green run DIRECTLY PRECEDED by red (a genuine fix). `median_files_reedited_after_red` is over
 *  the sessions that WENT RED; meaningless when `sessions_with_red_gate` is 0 (rendered as absence).
 *
 *  `median_runs_per_session`/`p90_runs_per_session` are NULL when `sessions_with_full_gate` is 0 — no
 *  distribution, and `0.0` would claim "ran the gate no times". */
export type GateStats = {
  sessions_with_full_gate: number
  full_gate_runs_total: number
  median_runs_per_session: number | null
  p90_runs_per_session: number | null
  sessions_over_one_run: number
  sessions_over_one_run_share: number | null
  rework_seconds: number
  rework_share: number
  gate_failures_total: number
  red_green_cycles_total: number
  sessions_with_red_gate: number
  files_reedited_after_red_total: number
  median_files_reedited_after_red: number
}

/** One repeatedly-read target. `sessions` beside `calls` tells one stuck runner (40 reads, 1 session)
 *  from a file every task must understand (a documentation gap). */
export type DuplicateTarget = {
  target: string
  calls: number
  sessions: number
}

/** One target that MORE THAN ONE PIPELINE STAGE of a single epic read. `stages`: distinct personas of
 *  one epic (4 = every stage). `epics`: how widely — 4 stages of 1 epic is an incident, 2 stages of 9 a
 *  standing gap. */
export type CrossStageTarget = {
  target: string
  stages: number
  epics: number
}

/** DUPLICATED WORK — reads of a target a session had already read in the same run.
 *
 *  `duplicate_read_calls` counts reads BEYOND the first within one session; `duplicate_read_share` is
 *  of all read-ish calls. CROSS-STAGE is a different measurement (a HANDOFF defect: a file read under
 *  two or more personas of ONE epic), kept visibly separate on the card because the fix differs.
 *
 *  `median_duplicate_targets` is per session over sessions that repeated at all, NULL when
 *  `sessions_with_duplicates` is 0. `median_cross_stage_targets` is per epic over epics that crossed. */
export type DuplicationStats = {
  sessions_with_duplicates: number
  median_duplicate_targets: number | null
  duplicate_read_calls: number
  duplicate_read_share: number
  top_targets: DuplicateTarget[]
  cross_stage_targets: number
  epics_with_cross_stage: number
  median_cross_stage_targets: number
  top_cross_stage: CrossStageTarget[]
}

/** SUBAGENT FAN-OUT — how much work was delegated, and how much of that was actually parallel.
 *
 *  A WAVE is a set of spawns issued together; `parallel_share` is the fraction issued in a message with
 *  more than one Agent block. `failed_spawns` is a cost of delegating (paid twice). `failure_share` is 0
 *  WHEN THERE WERE NO SPAWNS — not a measurement; the panel guards on `spawns_total`.
 *  `median_per_session`/`median_wave_size` are NULL when `spawns_total` is 0.
 *
 *  `subagent_seconds` is DELIBERATELY NOT COMPARABLE TO A PHASE TOTAL (subagents overlap, so it can
 *  exceed the window). `concurrency_ratio` is subagent wall-clock per unit of the runs' raw ELAPSED
 *  wall-clock (not the gap-capped attributed total); NULL when the denominator is unknown. */
export type SubagentStats = {
  sessions_with_subagents: number
  spawns_total: number
  median_per_session: number | null
  waves_total: number
  median_wave_size: number | null
  parallel_share: number
  failed_spawns: number
  failure_share: number
  subagent_seconds: number
  concurrency_ratio: number | null
}

/** ORIENTATION — tool calls a session made before its first edit (`Edit`/`Write`/`MultiEdit`/
 *  `NotebookEdit` or a task-artifact upload).
 *
 *  The median is over the sessions that edited; sessions that never did are COUNTED BESIDE IT, never
 *  folded in as 0. `median_calls_to_first_edit` is NULL when `sessions_with_edit` is 0. */
export type OrientationStats = {
  sessions_with_edit: number
  median_calls_to_first_edit: number | null
  sessions_without_edit: number
}

/** ONE SESSION'S ROW in the drill-down, capped at 200 by the server, ordered `total_seconds` DESC.
 *
 *  `phase_seconds` carries every phase the session ENTERED and only those: absent = never entered, `0`
 *  = entered (a last call has no following event to bound its span).
 *  `task_id` null = not a fleet task runner; `persona`/`org_repo` null = unresolved. Rendered as absences.
 *  The three counters are the row halves of the aggregate blocks; `red_green_cycles` rides the red
 *  count's cell title rather than a column of its own. */
export type SessionEfficiencyRow = {
  session_id: string
  task_id: number | null
  persona: string | null
  org_repo: string | null
  started_at: string
  ended_at: string
  total_seconds: number
  phase_seconds: Record<string, number>
  full_gate_runs: number
  subagent_spawns: number
  duplicate_read_calls: number
  cost_usd: string | null
  gate_failures: number
  red_green_cycles: number
  subagent_seconds: number
  /** NULL = the session never edited, rendered "no edit" — never 0. Optional: an older control plane
   *  omits it. */
  calls_to_first_edit?: number | null
  /** `'agent_reported'` = the agent's own figure, not priced from tokens; flagged beside the cost.
   *  Optional: an older control plane omits it. */
  cost_source?: 'tokens' | 'agent_reported' | null
}

/** THE WHOLE ANSWER — `SessionEfficiencyOut`.
 *
 *  `status: 'unavailable'` IS A SHIPPED STATE, NOT AN ERROR — the normal state on a local `./dc` stack or
 *  wherever the transcript bucket/analytics config is unset or Athena unreachable. 200 with a reason and
 *  every aggregate null; never render it as a failure.
 *
 *  `scanned_bytes`: Athena bills per byte scanned, so the operator sees the cost. `cached` + `generated_at`
 *  keep a cached answer from looking live. */
export type SessionEfficiency = {
  /** Inclusive UTC `YYYY-MM-DD`, echoed from the SERVER's window (so a clamp is visible), not the
   *  request. */
  since: string
  until: string
  generated_at: string
  status: 'ok' | 'unavailable'
  /** Non-null whenever `status` is not `ok`. Rendered verbatim — the server's own account. */
  unavailable_reason: string | null
  /** Set whenever `status` is not `ok`: `'unconfigured'` — no period helps; `'transient'` — retry, any
   *  period; `'window'` — another period helps. Optional because an older control plane omits it. */
  unavailable_kind?: 'unconfigured' | 'transient' | 'window' | null
  cached: boolean
  scanned_bytes: number
  /** Sessions covered. ZERO IS A REAL ANSWER (window empty), distinct from `unavailable`. */
  runs: number
  phases: PhaseStat[]
  /** `'priced'` = every model has a rate; `'partial'` = some spend EXCLUDED, so costs are floors;
   *  `'unpriced'` = no cost figure. Never a `$0.00` nobody computed. */
  cost_status: 'priced' | 'partial' | 'unpriced'
  /** Models that spent tokens with no row in the Settings pricing table. Fix by adding the row. */
  unpriced_models: string[]
  models: ModelStat[]
  /** Null in the `unavailable` state — every aggregate block is. */
  gate: GateStats | null
  duplication: DuplicationStats | null
  subagents: SubagentStats | null
  /** Null when `unavailable`; optional because an older control plane omits it. */
  orientation?: OrientationStats | null
  /** Null when `unavailable`; optional because an older control plane omits it. */
  genuine_exploration?: GenuineExploration | null
  sessions: SessionEfficiencyRow[]
}

/* ── The request ───────────────────────────────────────────────────────────────────────────────── */

/** The periods offered, in days. The route caps the window at 31 days
 *  (`app/session_efficiency.py::WINDOW_MAX_DAYS`, sized against the per-query scan cutoff) and REFUSES a
 *  wider one with 422 rather than clamping, so this list and that constant move together. 7 is the
 *  route's own default. */
export const PERIOD_DAYS = [7, 14, 30] as const
export type PeriodDays = (typeof PERIOD_DAYS)[number]

export const DEFAULT_PERIOD_DAYS: PeriodDays = 7

/** A `Date` as the route's `dt` partition key, IN UTC — a local formatter in UTC+13 would ask for
 *  tomorrow. */
export function utcDate(at: Date): string {
  return at.toISOString().slice(0, 10)
}

/** The window a period resolves to, INCLUSIVE at both ends and ending today (UTC) — hence `days - 1`. */
export function resolveWindow(days: number, now: Date = new Date()): { since: string; until: string } {
  const until = new Date(now)
  const since = new Date(now)
  since.setUTCDate(since.getUTCDate() - (days - 1))
  return { since: utcDate(since), until: utcDate(until) }
}

/** The one read. `since` and `until` are always both sent, or the server's default window would be
 *  printed as one the page was never told. */
export function getSessionEfficiency(window: { since: string; until: string }) {
  const params = new URLSearchParams({ since: window.since, until: window.until })
  return get<SessionEfficiency>(`/api/sessions/efficiency?${params}`)
}
