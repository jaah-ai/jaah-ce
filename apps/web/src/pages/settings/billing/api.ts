// Billing's ONE API client (Settings › Billing › Stripe, Products › Plans and Hosting products): the Stripe status (`GET /api/billing/status`), the plan catalogue
// (`routes/billing_plans.py`) and the hosting price catalogue (`/api/billing/rental-prices`). Operator-only.

import { del, get, patch, post } from '../../../api'
import type { PlanPrice } from '../../../lib/orgPlan'
import type { ProductLine, ProductLineIn } from '../../../lib/planProducts'

export const BILLING_STATUS_PATH = '/api/billing/status'

export type BillingStatus = {
  /** A Stripe credential is selected in `billing_stripe_credential_id`. */
  connected: boolean
  /** The account behind that credential has been identified. */
  pinned: boolean
  /** `acct_…`; a restricted key that cannot read its account reports `marker:cus_…` instead. */
  account_id: string | null
  /** true = Live, false = Test. */
  livemode: boolean | null
  /** ISO time of the last successful event poll. */
  last_poll_ok_at: string | null
  /** The error sentence when the last poll failed. */
  last_poll_error: string | null
  /** Orgs whose billing records belong to a different Stripe account than the connected one. */
  other_account_org_count: number
}

/** The account id a restricted key reports when it cannot read its own account. */
export const MARKER_PREFIX = 'marker:'

export const getBillingStatus = () => get<BillingStatus>(BILLING_STATUS_PATH)

export const BILLING_PLANS_PATH = '/api/billing/plans'

/** The caps a plan sets. null (or 0) = unlimited, except `github_runners`: 0 turns GitHub Runners off. No Agents cap:
 *  a plan's is its `max_agents` (the agents bought on a per-agent plan, `app/plan_terms.py::caps_of`). */
export const PLAN_LIMIT_KINDS = ['nodes', 'projects', 'users', 'github_runners'] as const
export type PlanLimitKind = (typeof PLAN_LIMIT_KINDS)[number]
export type PlanLimits = Record<PlanLimitKind, number | null>

/** EVERY plan switch (`app/plan_terms.py::SWITCHES`, the same order) with its code default. A switch's name is its
 *  plan field, its org override's key and its `effective_switches` key alike. */
export const PLAN_SWITCH_DEFAULTS = {
  hosting_allowed: true,
  compliance_enabled: false,
  advanced_compliance_enabled: false,
  nodevault_allowed: false,
  nodelink_allowed: false,
  merge_lane_enabled: false,
  microvm_allowed: true,
  playbooks_enabled: false,
  ubuntu_nodes_allowed: true,
  macos_nodes_allowed: false,
  windows_nodes_allowed: false,
  project_memory_enabled: false,
  slack_enabled: true,
  sso_enabled: false,
  scim_enabled: false,
  siem_export_enabled: false,
  data_residency_enabled: false,
  ip_allowlist_enabled: false,
  org_retention_enabled: false,
  staging_org_enabled: false,
  budgets_enabled: false,
  jaah_ai_enabled: false,
  audit_enabled: true,
  session_transcripts_enabled: true,
  aws_cloud_allowed: true,
  gcp_cloud_allowed: false,
  azure_cloud_allowed: false,
  tenant_email_allowed: true,
  org_templates_enabled: false,
} as const satisfies Record<string, boolean>
export type PlanSwitchField = keyof typeof PLAN_SWITCH_DEFAULTS
export const PLAN_SWITCH_FIELDS = Object.keys(PLAN_SWITCH_DEFAULTS) as PlanSwitchField[]

/** A switch as the plan's orgs get it: ON when the plan or a plan it includes has it ON; `inherited_from` (a plan
 *  key, `inherited_from_name` its name) names the included plan an ON comes from, null when it is the plan's own. */
export type EffectiveSwitch = { value: boolean; inherited_from: string | null; inherited_from_name: string | null }

/** The plan fields a plan may leave to `(default)` (`PlanAdmin.inherits`): one listed reads `(default)`'s value.
 *  `price` carries the monthly and yearly prices and per agent together; `providers` is the provider grid. */
export const INHERITABLE_FIELDS = [
  'price', 'trial_days', 'min_agents', 'max_agents', ...PLAN_LIMIT_KINDS, 'task_attachment_mb', 'attachment_file_mb',
  'org_attachment_gb', 'agent_requests_per_minute', 'cron_min_period_seconds', 'static_slots', 'duration_days',
  'then_plan_key', 'products', 'providers',
] as const
export type InheritableField = (typeof INHERITABLE_FIELDS)[number]

/** The reserved key of `(default)`: the fallback for every unset plan field. Never a plan of the list. */
export const DEFAULTS_KEY = '_default'
/** `(default)`'s name: the plans list's pinned row and its page's title. */
export const DEFAULTS_NAME = '(default)'

export type PlanAdmin = {
  key: string
  name: string
  blurb: string
  price: PlanPrice | null
  /** The yearly Price, sold beside the monthly one; null = monthly only. */
  yearly_price: PlanPrice | null
  /** The price is per agent: a purchase picks a count in [`min_agents` (null = 1), `max_agents` (null = no limit)]. */
  per_agent: boolean
  min_agents: number | null
  max_agents: number | null
  /** A per-agent plan's agent cap while its trial is live and `max_agents` is null; null = the server default
   *  (`BillingPlans.trial_agent_ceiling_default`). */
  trial_agent_ceiling?: number | null
  limits: PlanLimits
  /** Days an org stays on the plan once assigned; null = never ends. */
  duration_days: number | null
  /** Free trial a purchase opens, in days (once per org); null = none. */
  trial_days?: number | null
  /** The plan whose switches this one also turns on (transitively); null = none. Switches only — never caps or
   *  products. */
  includes_plan_key: string | null
  /** Where the plan's orgs move once its `duration_days` run out. */
  then_plan_key: string | null
  /** Every switch, included plans counted. */
  effective_switches: Record<PlanSwitchField, EffectiveSwitch>
  /** Static worktrees a project may hold on one node, for orgs on the plan; 0 = none. */
  static_slots: number
  /** What one task may store, and the largest file one upload may carry, MB. */
  task_attachment_mb?: number | null
  attachment_file_mb?: number | null
  /** What one org may store across its tasks, GB; the calls its nodes may make per minute; its fastest cron, seconds. */
  org_attachment_gb?: number | null
  agent_requests_per_minute?: number | null
  cron_min_period_seconds?: number | null
  /** The fields this plan reads from `(default)`; always empty on `(default)` itself. */
  inherits?: InheritableField[]
  /** The bundled products, each with the server's readable `label`. */
  products: ProductLine[]
  /** The plan's own provider grid, `[row, auth]` cells (`lib/providerGrid.ts`); null = it stores none (read through
   *  `(default)` when it inherits `providers`, else the code constant `PROVIDERS_SEED`). */
  providers?: string[][] | null
  self_serve: boolean
  active: boolean
  sort_order: number
  stripe_product_id: string | null
  stripe_price_id: string | null
  /** Orgs on this plan now. */
  member_count: number
  /** Why an org not on it cannot see it in its Change dialog (`org_plans.hidden_reason`); null = it can, or retired. */
  tenant_hidden: string | null
  /** No org ever used it and no other plan names it: it may be deleted (else only retired). */
  deletable: boolean
  /** Orgs with a live subscription on it or scheduled onto it: while any does, its price cannot be emptied. */
  subscriber_count: number
  /* Each switch: on a plan true On, false Off, null = `(default)`'s value (an included plan's On wins either way);
     always a boolean on `(default)`. */
} & Record<PlanSwitchField, boolean | null>

/** One switch's human label (`switch_catalogue`), in display order. */
export type SwitchLabel = { name: PlanSwitchField; label: string }

/** `plans` never holds `(default)`; it is `defaults`. */
export type BillingPlans = {
  billing_connected: boolean
  switch_catalogue: SwitchLabel[]
  plans: PlanAdmin[]
  defaults: PlanAdmin
  /** The trial agent ceiling a plan left blank gets (`app/plan_terms.py::TRIAL_AGENT_CEILING_DEFAULT`). */
  trial_agent_ceiling_default: number
}

/** `unit_amount` is integer cents; sending it while billing is not connected is a 409. */
export type PlanCreate = {
  key: string
  name: string
  blurb?: string
  unit_amount?: number | null
  /** Cents per year (per agent on a per-agent plan); needs a monthly price. */
  yearly_unit_amount?: number
  per_agent?: boolean
  /** Explicit null clears: min 1, max no limit. */
  min_agents?: number | null
  max_agents?: number | null
  /** Explicit null clears: the server default. */
  trial_agent_ceiling?: number | null
  limits?: PlanLimits
  /** null = never ends. */
  duration_days?: number | null
  /** 0 or null = no trial. */
  trial_days?: number | null
  /** Explicit null clears the link. A duration needs a then plan (422 "Needs a then plan."). */
  includes_plan_key?: string | null
  then_plan_key?: string | null
  static_slots?: number
  /** Explicit null clears each back to `(default)`'s. */
  task_attachment_mb?: number | null
  attachment_file_mb?: number | null
  org_attachment_gb?: number | null
  agent_requests_per_minute?: number | null
  cron_min_period_seconds?: number | null
  /** Replaces the whole list. Refused on `(default)`. */
  inherits?: InheritableField[]
  /** Replaces the whole list. */
  products?: ProductLineIn[]
  /** The provider grid's `[row, auth]` cells, the whole list. */
  providers?: string[][]
  self_serve?: boolean
  sort_order?: number
} & Partial<Record<PlanSwitchField, boolean | null>>
/** A price can be changed (a new Stripe Price) or cleared (null; the monthly one only while nobody subscribes). */
export type PlanPatch = Partial<Omit<PlanCreate, 'key' | 'unit_amount' | 'yearly_unit_amount'>> & {
  /** null = no price: both Stripe Prices archived (409 while anyone subscribes). */
  unit_amount?: number | null
  yearly_unit_amount?: number | null
  active?: boolean
}

export const PROVIDER_GRID_PATH = '/api/provider-grid'

/** One auth-type column of the provider grid. */
export type ProviderGridColumn = { key: string; label: string }
/** One row: `key` as stored in a cell, the columns it takes (every other column is absent, not off), and a `note`
 *  (a registry agent the live registry no longer lists). */
export type ProviderGridRow = { key: string; label: string; auth: string[]; note?: string | null }
export type ProviderGridGroup = { key: string; label: string; rows: ProviderGridRow[] }
/** `GET /api/provider-grid` (`routes/provider_grid.py`): the rows and columns a plan's or an org's `providers` cells
 *  are picked from. `registry_unavailable`: the ACP registry could not be read, so its rows are only the stored ids. */
export type ProviderGridCatalogue = {
  columns: ProviderGridColumn[]
  groups: ProviderGridGroup[]
  registry_unavailable?: boolean
}

export const getProviderGrid = () => get<ProviderGridCatalogue>(PROVIDER_GRID_PATH)

/** The catalogue; a body without a plans list is refused here, so no reader draws a malformed answer as empty. */
export async function listBillingPlans(): Promise<BillingPlans> {
  const next = await get<BillingPlans>(BILLING_PLANS_PATH)
  if (!Array.isArray(next?.plans)) throw new Error('Unexpected response.')
  return next
}
export const createBillingPlan = (body: PlanCreate) => post<PlanAdmin>(BILLING_PLANS_PATH, body)
export const updateBillingPlan = (key: string, body: PlanPatch) =>
  patch<PlanAdmin>(`${BILLING_PLANS_PATH}/${encodeURIComponent(key)}`, body)

/** One bundled node configuration a change would provision or release, across `count` orgs' lines. */
export type DryRunLine = { fingerprint: string; label: string; count: number }

/** What an edit would do to the plan's members; nothing is written. */
export type PlanDryRun = {
  members: number
  /** Orgs whose plan would end at once. */
  pausing: number
  overrides_cleared: string[]
  provision: DryRunLine[]
  release: DryRunLine[]
  /** Live subscribers a per-agent edit leaves outside the range (all of them when Priced per agent flips); 0 on an
   *  org's plan-move preview. */
  agents_outside?: number
}

/** `PATCH …?dry_run=1`: the edit's effect on the plan's members, nothing saved. */
export const previewBillingPlan = (key: string, body: PlanPatch) =>
  patch<PlanDryRun>(`${BILLING_PLANS_PATH}/${encodeURIComponent(key)}?dry_run=1`, body)
/** 409 with a sentence while orgs are subscribed. */
export const retireBillingPlan = (key: string) =>
  post<PlanAdmin>(`${BILLING_PLANS_PATH}/${encodeURIComponent(key)}/retire`)
/** Only a `deletable` plan; 409 with a sentence otherwise. Its Stripe Prices are archived. */
export const deleteBillingPlan = (key: string) => del(`${BILLING_PLANS_PATH}/${encodeURIComponent(key)}`)

export const HOSTING_PRICES_PATH = '/api/billing/rental-prices'

/** `instance` = a Dedicated host's instance type, `storage` = a size in GB, `shared` = a Shared tier, `windows` = the
 *  monthly Windows license on a Dedicated host of that instance type (key = the type). */
export type HostingKind = 'instance' | 'storage' | 'shared' | 'windows'
/** `live_cast`: Live Cast's one per-project price (key `project`) — Products › Live Cast, never a hosting list. */
export type RentalPriceKind = HostingKind | 'live_cast'

export type HostingPriceAdmin = {
  id: string
  kind: RentalPriceKind
  /** The instance type, the size in GB, or the tier's key. Fixed once created. */
  key: string
  name: string
  /** Integer cents per month. */
  monthly_cents: number
  active: boolean
  sort_order: number
  vcpu: number | null
  ram_gb: number | null
  /** A Shared tier's disk in GB: the cap its Shared nodes bind to. Null on instance and storage rows. */
  disk_gb: number | null
  /** Sells in Stripe at this price on the connected account. */
  synced: boolean
  /** An instance row: whether the allow-list still allows its type (false = stale). Null on storage and Shared. */
  allowed: boolean | null
  /** An instance row: `vcpu`/`ram_gb` are the allowed type's own specs, not the operator's. */
  specs_from_type?: boolean
}

export type HostingPrices = { billing_connected: boolean; prices: HostingPriceAdmin[] }

/** Saves without a Stripe connection too: the row is pushed on a later save or by the nightly reconcile. */
export type HostingPriceCreate = {
  /** `live_cast`: key and name are the server's own. */
  kind: RentalPriceKind
  key?: string
  name?: string
  monthly_cents: number
  active?: boolean
  sort_order?: number
  vcpu?: number
  ram_gb?: number
  disk_gb?: number
}
export type HostingPricePatch = Partial<Pick<HostingPriceAdmin, 'name' | 'monthly_cents' | 'active' | 'sort_order' | 'vcpu' | 'ram_gb' | 'disk_gb'>>

export const listHostingPrices = () => get<HostingPrices>(HOSTING_PRICES_PATH)
export const createHostingPrice = (body: HostingPriceCreate) => post<HostingPriceAdmin>(HOSTING_PRICES_PATH, body)
export const updateHostingPrice = (id: string, body: HostingPricePatch) =>
  patch<HostingPriceAdmin>(`${HOSTING_PRICES_PATH}/${encodeURIComponent(id)}`, body)
