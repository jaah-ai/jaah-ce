// The Orgs surface's API client, so page and dialog agree on paths. `Org` lives in `src/types.ts`; this
// module owns the REQUEST shapes, each encoding a server decision.

import { del, get, patch, post, put } from '../../api'
import { countOf } from '../../lib/humanize'
import { slugFrom } from '../../lib/slug'
import type { OrgLimit as PlanLimit } from '../../lib/orgPlan'
import type { ConfirmOptions } from '../../components/ConfirmDialog'
import type { OperatorSetting, Org, SsoProvider } from '../../types'
import type { OrgBundle } from '../../lib/bundles'
import type { ProductLine, ProductLineIn } from '../../lib/planProducts'
import type { PlanDryRun, PlanSwitchField } from '../settings/billing/api'

/** A new tenant. `slug` and `plan_key` (an active plan, never `_default`) are REQUIRED. */
export type OrgCreateBody = {
  slug: string
  name: string
  description: string
  plan_key: string
}

/* A PARTIAL update: only present keys are touched. */
export type OrgUpdateBody = {
  /* A tenant org's handle, operator only: the server moves its tailnet
   * devices to `tag:org-<slug>` (`routes/orgs.py::_rename_slug`). Refused for the operator plane. */
  slug?: string
  name?: string
  description?: string
  enabled?: boolean
  /* An Org Owner/Admin may set this and the SSO fields on their own org; `null` = none. */
  default_role?: string | null
  /* The operator's isolation exception for a tenant org (`app/tenant_defaults.py`); operator only. */
  colocation_allowed?: boolean
  /* SSO-only: a provider with its tenant turns it on, `null` off. Changing either clears the attestation
   * unless `sso_mfa_attested` is sent true with it (`routes/orgs.py::_apply_sso`). */
  sso_provider?: SsoProvider | null
  sso_tenant?: string | null
  sso_mfa_attested?: boolean
  /* Members sign in without a two-factor code (a demo/recording org); operator only, never an
   * operator-plane org. Off signs every member out. */
  mfa_skip?: boolean
  /* HIPAA mode, operator only: on needs a BAA effective date (`YYYY-MM-DD`; 422 without one). */
  hipaa_enabled?: boolean
  hipaa_baa_effective_date?: string | null
  /* The org's MicroVM size (`app/microvm_sizing.py`): an Org Owner/Admin on their own org, or the operator;
   * `null` clears one (the system value applies). */
  microvm_vcpus?: number | null
  microvm_ram_mib?: number | null
  microvm_disk_gib?: number | null
}

/* `GET /api/orgs` lives in `lib/orgs.ts` (`ui/OrgSelect` needs it and `ui/**` may not import `pages/**`).
 * Not re-exported here: one fact, one import path (`pages/nodes/nodeGroups/api.ts`). */

export const createOrg = (body: OrgCreateBody) => post<Org>('/api/orgs', body)

/* A generated slug for a new org's name — the same one sign-up gives (`app/org_slug.py`, LLM-backed with a
 * deterministic fallback), free at the time of asking. Operator only. */
export const suggestOrgSlug = (name: string) =>
  get<{ slug: string }>(`/api/orgs/slug-suggestion?name=${encodeURIComponent(name)}`)

/** `Org.enabled` is read on dispatch (`app/pool_state.py::live_nodes` / `live_nodes_for_project`): a
 *  disabled org gets no new work; in-flight work settles. It is not read at login. State both halves. */
export const LIFECYCLE_NOTE =
  'Disabling takes an org out of service: no new work is dispatched to its nodes, and work already in '
  + 'flight settles under the rules it started with. Sign-in is not gated by it yet. Nothing is deleted '
  + 'by it.'

/** Disable's confirm, one copy for the Customers list and the customer page (`useConfirm`). */
export function disableOrgConfirm(org: Pick<Org, 'name' | 'slug' | 'user_count'>): ConfirmOptions {
  const bound = org.user_count === 0
    ? 'No users are bound to it.'
    : `${org.user_count} user${org.user_count === 1 ? ' is' : 's are'} bound to it.`
  return {
    title: 'Disable org',
    identity: `${org.name} · ${org.slug}`,
    message: `Disable ${org.name}? ${bound} ${LIFECYCLE_NOTE}`,
    confirmLabel: 'Disable',
    destructive: true,
  }
}

export const updateOrg = (id: string, body: OrgUpdateBody) => patch<Org>(`/api/orgs/${encodeURIComponent(id)}`, body)

/* One org: an operator any org, an Org Owner/Admin their own (`routes/orgs.py::get_org`). */
export const getOrg = (id: string) => get<Org>(`/api/orgs/${encodeURIComponent(id)}`)

/* A self-service org's admission (operator-only). Each answers the updated `Org`, or a 409 unless the org is
 * still pending. A rejection's `reason` is shown to its members at sign-in; the server caps it at
 * `REJECT_REASON_MAX` (422 past it). An approval's `send_welcome` mails the applicant; the answer's
 * `welcome_outcome` says what that send did. */
export const REJECT_REASON_MAX = 200
/* `planKey` (any active plan) replaces the plan applied for; absent keeps it. */
export const approveOrg = (id: string, sendWelcome = true, planKey?: string) =>
  post<Org>(`/api/orgs/${encodeURIComponent(id)}/approve`, { send_welcome: sendWelcome, ...(planKey ? { plan_key: planKey } : {}) })
/* Sends (or resends) the welcome email; answers the `Org` with `welcome_outcome`. A 409 unless the org is an
 * active self-service one. */
export const sendWelcomeEmail = (id: string) => post<Org>(`/api/orgs/${encodeURIComponent(id)}/welcome-email`)
export const rejectOrg = (id: string, reason?: string) =>
  post<Org>(`/api/orgs/${encodeURIComponent(id)}/reject`, reason ? { reason } : {})

/** One of an org's caps or abuse quotas (`routes/orgs.py::LimitOut`): the identity payload's cap (`lib/orgPlan.ts`)
 * plus who set it: this org's override, the agents it bought on a per-agent plan (`purchase`) or its live trial's lifted
 * cap (`trial`, Agents and the Active worktrees that follow it), or else its plan (a
 * cap) or the platform knob (an abuse quota, never a plan's); `override` is the org's own value as stored (null = it follows the plan or the platform `default`,
 * 0 = no limit, off on GitHub Runners). */
export type OrgLimit = PlanLimit & {
  source: 'default' | 'plan' | 'override' | 'purchase' | 'trial'
  override: number | null
  /** What clearing `override` falls back to: null or 0 = no limit (0 = off on GitHub Runners). */
  default: number | null
  /** Whose value `default` is: the plan's, or `(default)`'s where the plan leaves it unset. */
  fallback_source: 'plan' | 'default'
  /** The largest override the server takes for this kind; 0 means no limit. */
  maximum: number
  /** What the numbers count in ("MB", "GB"); empty or absent for a plain count. */
  unit?: string
}

/** The per-file cap (`app/org_quotas.py::FILE_SIZE`): a ceiling on one file, so its usage is the largest stored file
 *  and 0 means `maximum`, never no limit. */
export const FILE_SIZE_KIND = 'attachment_file_mb'
/** The concurrent-agents cap (`app/org_limits.py`): on a per-agent plan, the agents bought. */
export const RUNNER_SLOTS = 'runner_slots'
/** The per-org agent request budget (`app/org_quotas.py::AGENT_REQUESTS`), counted per minute. */
export const REQUESTS_KIND = 'agent_requests_per_minute'
/** The per-org task storage quota (`app/org_quotas.py::TASK_ATTACHMENT`), in MB. */
export const TASK_STORAGE_KIND = 'task_attachment_mb'
/** The org storage quota (`app/org_quotas.py::ORG_ATTACHMENT`), in GB. */
export const ORG_STORAGE_KIND = 'org_attachment_gb'

// Operator only, read and write: limits are never a tenant's knob. PUT: `{kind: n}` sets an override,
// `{kind: null}` clears it back to the plan (a quota: the platform); the answer is every cap and quota.
export const getOrgLimits = (id: string) => get<OrgLimit[]>(`/api/orgs/${encodeURIComponent(id)}/limits`)
export const putOrgLimits = (id: string, body: Record<string, number | null>) =>
  put<OrgLimit[]>(`/api/orgs/${encodeURIComponent(id)}/limits`, body)

// Operator only, read and write (`routes/orgs.py::put_org_settings`): the org-tier settings as they govern
// one org. PUT: `{key: value}` sets the org's override, `{key: null}` clears it so the org inherits again;
// the answer is every org-tier setting.
export const getOrgSettings = (id: string) => get<OperatorSetting[]>(`/api/orgs/${encodeURIComponent(id)}/settings`)
export const putOrgSettings = (id: string, values: Record<string, OperatorSetting['value'] | null>) =>
  put<OperatorSetting[]>(`/api/orgs/${encodeURIComponent(id)}/settings`, { values })

/** An org's cron minimum period (`routes/orgs.py`), all seconds. The chain: the operator's floor, then the
 * operator's per-org `override` (null = it follows `fallback`: a tenant org's plan's, else `(default)`'s, or
 * `operator_floor` for an operator-plane one; `fallback_source` says which), then the org admin's raise-only
 * `own`. `effective` is
 * what the scheduler enforces. */
export type CronMinPeriod = {
  operator_floor: number
  tenant_default: number
  fallback: number
  fallback_source: 'operator' | 'plan' | 'default'
  override: number | null
  upstream: number
  upstream_source: 'operator' | 'plan' | 'default' | 'org_override'
  own: number | null
  effective: number
}

/** The server's ceiling for both `override` and `own`. */
export const CRON_MIN_PERIOD_MAX = 86400

/* PATCH: a field not sent is untouched, `null` clears. `override` is the operator's (403 otherwise), within
 * `operator_floor`..max; `own` is the org admin's, within `upstream`..max. A 422's detail is a sentence. */
export type CronMinPeriodBody = { override?: number | null; own?: number | null }

// Read: an operator on any org, an Org Owner/Admin on their own.
export const getOrgCronMinPeriod = (id: string) => get<CronMinPeriod>(`/api/orgs/${encodeURIComponent(id)}/cron-min-period`)
export const patchOrgCronMinPeriod = (id: string, body: CronMinPeriodBody) =>
  patch<CronMinPeriod>(`/api/orgs/${encodeURIComponent(id)}/cron-min-period`, body)

/** The one bound a cron-minimum draft fails, named; nothing while it passes or is still empty. `floor` is the
 *  tier's lowest value: the operator floor for the operator's override, the upstream for an org's own. */
export function cronPeriodError(trimmed: string, parsed: number | null, floor: number): string | undefined {
  if (trimmed === '') return undefined
  if (parsed === null) return 'Whole seconds'
  if (parsed < floor) return `At least ${floor}`
  if (parsed > CRON_MIN_PERIOD_MAX) return `At most ${CRON_MIN_PERIOD_MAX}`
  return undefined
}

/** A period as words, in its largest whole unit: `10 seconds`, `1 hour`, `90 seconds`. */
export function periodWords(seconds: number): string {
  const units: [number, string][] = [[86400, 'day'], [3600, 'hour'], [60, 'minute'], [1, 'second']]
  const [size, unit] = units.find(([n]) => seconds >= n && seconds % n === 0) ?? [1, 'second']
  return countOf(seconds / size, unit)
}

/** One email domain of an org (`routes/orgs.py::OrgDomainOut`). */
export type OrgDomain = { domain: string; created_at: string | null; created_by: string | null }

// Read: Org Admin. Add/remove: operator only (no DNS proof, so the operator is the proof).
export const listOrgDomains = (id: string) => get<OrgDomain[]>(`/api/orgs/${encodeURIComponent(id)}/domains`)
export const addOrgDomain = (id: string, domain: string) => post<OrgDomain>(`/api/orgs/${encodeURIComponent(id)}/domains`, { domain })
export const removeOrgDomain = (id: string, domain: string) =>
  del(`/api/orgs/${encodeURIComponent(id)}/domains/${encodeURIComponent(domain)}`)

/* Delete: `DELETE /api/orgs/{org_id}` answers 204 (`del`'s `void` default is the shape). Preconditions are
 * the server's (an enabled node, a non-`free` slot, a running task — rows the roster lacks); each refusal
 * is a 409 whose `detail` sentence the caller renders verbatim. */
export const deleteOrg = (id: string) => del(`/api/orgs/${encodeURIComponent(id)}`)

/* Delete account: the Owner's own org, from Billing (`routes/orgs.py::delete_own_org`) — the same soft delete. The
 * check names the org to type back and whether a 2FA code is due; the delete answers 204 and signs every member out. */
export type OwnOrgDelete = { name: string; needs_code: boolean }
const OWN_ORG_DELETE_PATH = '/api/org/delete'
export const getOwnOrgDelete = () => get<OwnOrgDelete>(OWN_ORG_DELETE_PATH)
export const deleteOwnOrg = (body: { name: string; code: string }) => post<void>(OWN_ORG_DELETE_PATH, body)

/* Delete is SOFT: the org is hidden, its members' sign-in refused, its data kept, its billing ended. Restore
 * brings it back still disabled (a 409 unless deleted). */
export const restoreOrg = (id: string) => post<Org>(`/api/orgs/${encodeURIComponent(id)}/restore`)

/** What a Compliance Delete would erase (`execute: false`; `app/tenancy/offboard.py::Census.counts`); any
 *  `blockers` refuse the execute. */
export type ComplianceCensus = {
  execute: false
  org: { id: string; slug: string; name: string }
  blockers: string[]
  stores: {
    /** Rows per table. */
    db_rows: Record<string, number>
    s3_objects: { attachments: number; transcript_sessions: number }
    /** One label per slot, shared account, and account dir on a node. */
    worktrees: string[]
    shared_store: string[]
    account_dirs: string[]
    /** Slack/forge credentials per type. */
    installs: Record<string, number>
  }
}

/** The executed purge's offboarding record. */
export type ComplianceDeleteDone = { execute: true; record: { complete: boolean } & Record<string, unknown> }

/* A soft-deleted org's permanent purge. `execute: true` needs the operator's own TOTP `code`: a 403 names a
 * missing or wrong code, a 409 a live org or a blocker — or, stopped midway, an object detail `{message, record}`. */
export function complianceDeleteOrg(id: string, body: { code: string; execute: false }): Promise<ComplianceCensus>
export function complianceDeleteOrg(id: string, body: { code: string; execute: true }): Promise<ComplianceDeleteDone>
export function complianceDeleteOrg(id: string, body: { code: string; execute: boolean }): Promise<ComplianceCensus | ComplianceDeleteDone> {
  return post(`/api/orgs/${encodeURIComponent(id)}/compliance-delete`, body)
}

/** Form fields a refusal can be attributed to. */
export type OrgFormField = 'slug' | 'name' | 'description' | 'plan_key'

/* Which field a refusal belongs to, from the server's message (`pages/settings/tags/api.ts` has the full
 * reasoning): `ui/FormField` is the only place a field message renders; unrecognised → `null` (shown at the
 * top). Presentation, never a gate. ORDER IS LOAD-BEARING: the duplicate-slug phrase is matched before the
 * `^name ` arm could claim it. */
export function fieldForDetail(detail: string): OrgFormField | null {
  if (/already answers to the slug/.test(detail) || /^the slug /.test(detail)) return 'slug'
  if (/already has the slug/.test(detail) || /slug (is also its tailnet tag|cannot be changed)/.test(detail)) return 'slug'
  if (/^slug /.test(detail)) return 'slug'
  if (/^name /.test(detail)) return 'name'
  if (/^description /.test(detail)) return 'description'
  return null
}

/* The slug pre-filled from a name — a DERIVATION, never a validation (the grammar is the server's:
 * `routes/commands.py::_SLUG_RE`, enforced at `routes/orgs.py`). Stops once the operator edits the slug
 * (`OrgDialog`): the handle is permanent. */
export const slugFromName = (name: string) => slugFrom(name, 32)

export type { BundleStatus, OrgBundle } from '../../lib/bundles'

// Operator only. Retry answers every bundle again; a 409 with a sentence unless the copy is `failed` or `removed`
// (or another retry holds it). It launches a machine, so it can take several seconds.
export const retryOrgBundle = (id: string, bundleId: string) =>
  post<OrgBundle[]>(`/api/orgs/${encodeURIComponent(id)}/bundles/${encodeURIComponent(bundleId)}/retry`)

/** One overridable plan term: the org's effective `value`, whether it is the org's own, and its plan's. */
export type Override<T> = { value: T; overridden: boolean; plan_value: T }
/** A plan switch's term: `inherited_from` names the plan (by name) the plan's ON comes from through an include;
 *  null (or absent, from an older server) when the plan has it ON itself, or OFF. */
export type SwitchOverride = Override<boolean> & { inherited_from?: string | null }

/** One org's plan as the operator runs it (`routes/orgs.py::OrgPlanOut`). `plan_ends_at` null = never ends;
 * `price` is all null on a plan Stripe does not sell. `choices` is every active plan, self-serve or not;
 * `stripe_plan_*` is what Stripe last entitled (null = not synced); on a plan Stripe sells (`plan_sold`), a `stripe_plan_key` unlike `plan_key` means the push is still
 * in flight. An unsold plan (Trial) is never entitled: its `stripe_plan_key` stays `-`. */
export type OrgPlan = {
  plan_key: string
  plan_name: string
  pending_plan_key: string | null
  pending_plan_name: string | null
  plan_started_at: string
  plan_ends_at: string | null
  plan_ended: boolean
  /** The plan is priced per agent: its agent bounds apply. */
  per_agent: boolean
  /** Every plan switch (`settings/billing/api.ts::PLAN_SWITCH_FIELDS`), plus the end, the plan after it, products,
   *  price and the agent counts. `then_plan` null = none; `max_agents` null = no upper bound. */
  overrides: {
    ends_at: Override<string | null>
    then_plan: Override<string | null> & { value_name: string | null; plan_value_name: string | null }
    products: Override<ProductLine[]>
    price: Override<number | null>
    min_agents: Override<number | null>
    max_agents: Override<number | null>
    static_slots: Override<number | null>
    /** The provider grid's `[row, auth]` cells (`lib/providerGrid.ts`); absent from an older server. */
    providers?: Override<string[][]>
  } & Record<PlanSwitchField, SwitchOverride>
  /** `trialable`: Stripe sells it above $0, so a move to it may open a free trial; `trial_days` its own trial length
   *  (null = none). Both absent from an older server. */
  choices: { key: string; name: string; trialable?: boolean; trial_days?: number | null }[]
  stripe_plan_key: string | null
  stripe_plan_name: string | null
  billing_connected: boolean
  plan_sold: boolean
  /** Stripe's final refusal of the push to `plan_key` (`org_plan_admin.push_refusal`): the push will not land, so the
   *  tab says "Not synced" with it. null while it may still land; absent from an older server. */
  push_refused?: string | null
  /** Slack messages dropped because the plan leaves Slack out (`slack_egress.record_plan_drop`); absent from an
   *  older server. */
  slack_plan_drops?: number
}

/* PUT sends only what changes. A `plan_key` change clears every override, caps included (DA-4), unless
 * `keep_overrides`. `plan_started_at` re-dates the plan's start (not in the future, not before the org existed).
 * `ends_at.mode` `plan` resets the end; a plan switch (`PLAN_SWITCH_FIELDS`), `products` or `providers` null resets to the plan's;
 * `custom_unit_amount` (cents, null = the plan's price) is sent on its own — the server 422s it with anything else.
 * `then_plan.mode` `plan` resets it; the agent counts null reset (`max_agents` 0 = no upper bound). The server 422s
 * max under min, a then plan that is the org's own, an unknown plan. */
export type OrgPlanBody = {
  plan_key?: string
  keep_overrides?: boolean
  plan_started_at?: string
  ends_at?: { mode: 'plan' | 'date' | 'never'; date?: string }
  then_plan?: { mode: 'plan' | 'none' | 'key'; plan_key?: string }
  /** The free trial's end, sent to Stripe with the plan; null ends a running one now. */
  trial_end?: string | null
  /** False keeps the paid time a new trial covers instead of refunding it (default: refunded). */
  refund_prepaid?: boolean
  min_agents?: number | null
  max_agents?: number | null
  static_slots?: number | null
  products?: ProductLineIn[] | null
  /** The provider grid's cells, the whole list; null = back to the plan's. Accepted on the operator's own org too. */
  providers?: string[][] | null
  custom_unit_amount?: number | null
} & Partial<Record<PlanSwitchField, boolean | null>>

export const getOrgPlan = (id: string) => get<OrgPlan>(`/api/orgs/${encodeURIComponent(id)}/plan`)
export const updateOrgPlan = (id: string, body: OrgPlanBody) =>
  put<OrgPlan>(`/api/orgs/${encodeURIComponent(id)}/plan`, body)
/** `?dry_run=1`: what the change would do (members = 1), nothing written. */
export const previewOrgPlan = (id: string, body: OrgPlanBody) =>
  put<PlanDryRun>(`/api/orgs/${encodeURIComponent(id)}/plan?dry_run=1`, body)
