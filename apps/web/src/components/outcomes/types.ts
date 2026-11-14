/* The outcome ledger's read API — the TS twin of the API's outcome response models (the Command center's outcomes
 * route). The wire fixture `outcomes.fixture.json` (beside the Command center's outcome tabs) pins both sides: the
 * API's outcome-metrics test validates it against the pydantic models and `types.test.ts` checks it against the key
 * lists below. */

export type Period = '30d' | '90d' | '365d' | 'all'

/** `group=org` is the organization roll-up: one group over the whole scope. */
export const GROUPS = ['agent', 'model', 'config', 'project', 'org'] as const
export type Group = (typeof GROUPS)[number]

/** A rate with its Wilson 95% interval; per-100 rates carry the interval scaled to per-100. */
export interface Rate {
  value: number | null
  lo: number | null
  hi: number | null
  n: number
}

export interface Rework {
  value: number | null
  hours: number | null
  n: number
}

export interface Completion {
  median_h: number | null
  p90_h: number | null
  agentic_median_h: number | null
  n: number
}

/** One detecting lane's credit: the security findings it caught in the group's changes. */
export interface DetectorCredit {
  detector: string
  findings: number
}

/** Security findings per 100 judged dispatches, debited to the group that wrote the change. */
export interface SecurityRate extends Rate {
  by_detector: DetectorCredit[]
}

export interface CostPerSuccess {
  value_usd: number | null
  n: number
}

export interface Metrics {
  success: Rate
  first_pass: Rate
  defects_per_100: Rate
  rework: Rework
  completion: Completion
  cost_per_success: CostPerSuccess
  security_per_100: SecurityRate
}

export const METRIC_KEYS = [
  'success', 'first_pass', 'defects_per_100', 'rework', 'completion', 'cost_per_success', 'security_per_100',
] as const satisfies readonly (keyof Metrics)[]

export interface BtRating {
  rating: number
  /** Null when the rating rests on too few duels for an interval. */
  lo: number | null
  hi: number | null
  n_duels: number
}

/** One agent, model, configuration or project. `label` is the full human name — wrap it, never truncate. */
export interface OutcomeGroup {
  key: string
  label: string
  n_final: number
  ranked: boolean
  insufficient: boolean
  metrics: Metrics | null
  bt: BtRating | null
}

export const GROUP_KEYS = [
  'key', 'label', 'n_final', 'ranked', 'insufficient', 'metrics', 'bt',
] as const satisfies readonly (keyof OutcomeGroup)[]

export interface ProfilesResponse {
  group: Group
  groups: OutcomeGroup[]
}

export interface RankedGroup extends OutcomeGroup {
  rank: number
}

export interface LeaderboardResponse {
  min_n: number
  configs: RankedGroup[]
}

export interface Stratum {
  persona: string
  bucket: string
}

/** `shared_strata: []` means no stratum in common: every `metrics` is null. */
export interface CompareResponse {
  shared_strata: Stratum[]
  configs: OutcomeGroup[]
}

export interface IndexCell extends OutcomeGroup {
  below_threshold: false
}

/** A cell under 5 orgs or 100 outcomes: no number at all. */
export interface SuppressedCell {
  key: string
  label: string
  below_threshold: true
}

export const SUPPRESSED_CELL_KEYS = ['key', 'label', 'below_threshold'] as const satisfies readonly (keyof SuppressedCell)[]

export interface AgentIndexResponse {
  min_orgs: number
  min_outcomes: number
  cells: (IndexCell | SuppressedCell)[]
  /** Sums over emitted cells only. */
  totals: { scope: 'emitted_cells_only'; cells: number; n_final: number }
}

export interface OutcomeHead {
  head_sha: string
  ref: string | null
  pushed_at: string
  source: string
  after_enqueue: boolean
  verdict: string
  verdict_source: string | null
  verdict_at: string | null
}

export interface OutcomeLink {
  id: string
  kind: string
  evidence_kind: string
  evidence_ref: string
  evidence_title: string | null
  confidence: number
  defect_state: string
  state: string
  /** `kind:state:confidence`, as the CSV export writes it. */
  label: string
  /** Who confirmed or dismissed it; null while proposed. */
  decided_by_name: string | null
  decided_at: string | null
  created_at: string
}

export interface OutcomeRow {
  id: string
  dispatch_seq: number
  persona: string
  agent_kind: string
  provider: string | null
  /** The agent's and model's human names, as Performance names them. */
  agent_label: string
  model_label: string
  agent_id: string | null
  agent_commit: string | null
  model: string | null
  model_family: string | null
  auth_kind: string | null
  review_tier: string
  node_hosting: string | null
  node_isolation: string | null
  config_key: string
  outcome_state: string
  end_kind: string | null
  end_reason: string | null
  excluded: boolean
  excluded_reason: string | null
  backfilled: boolean
  shadow: boolean
  task_ready_at: string | null
  dispatched_at: string
  first_active_at: string | null
  pr_opened_at: string | null
  enqueued_at: string | null
  merged_at: string | null
  merge_sha: string | null
  pr_url: string | null
  deployed_at: string | null
  survival_ends_at: string | null
  ended_at: string | null
  lane_failures: number | null
  pushes_after_enqueue: number | null
  nonrunner_commits: number | null
  security_findings: number | null
  tokens_in: number | null
  tokens_out: number | null
  cost_usd: number | null
  cost_status: string | null
  node_minutes: number | null
  lane_minutes: number | null
  additions: number | null
  deletions: number | null
  files_changed: number | null
  language: string | null
  clarity_score: number | null
  complexity_bucket: string | null
  heads: OutcomeHead[]
  links: OutcomeLink[]
}

/** The benchmark duel a task is in (`routes/outcomes.py::DuelOut`): `role` is this task's side; `result` the winning
 *  side, null until decided. */
export interface BenchmarkDuel {
  id: string
  role: 'origin' | 'shadow'
  state: 'running' | 'reviewing' | 'decided' | 'void'
  result: 'origin' | 'shadow' | 'tie' | null
  void_reason: string | null
  origin_task_id: number
  shadow_task_id: number | null
  shadow_pr_url: string | null
  shadow_pr_closed: boolean
  origin_label: string
  shadow_label: string
  gate_origin: string | null
  gate_shadow: string | null
  review_reason: string | null
  diff_distance: number | null
  decided_at: string | null
}

export interface TaskOutcomesResponse {
  can_decide: boolean
  rows: OutcomeRow[]
  duel?: BenchmarkDuel | null
}
