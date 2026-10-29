// The Jaah AI balance's wire shapes and words: a prepaid USD wallet for the Jaah-provided Bedrock account.
// The tenant's half is `GET/PUT /api/org/jaah-ai…` (Org Owner/Admin; an operator-plane caller gets 404); the
// operator's is `…/api/billing/orgs/{org_id}/jaah-ai…` (`finance:view` reads, `finance:edit` writes).
// Money on the wire is integer micro-USD (1 USD = 1_000_000); markup is basis points (100 bps = 1%).
// React-free.

import { get, post, put } from '../api'
import { moneyText, shortDate } from './billing'
import { compactCount, groupedCount } from './humanize'

export type FloorState = 'ok' | 'low' | 'floor'

export type LedgerKind =
  | 'signup_credit'
  | 'operator_credit'
  | 'operator_debit'
  | 'topup'
  | 'auto_recharge'
  | 'refund'
  | 'spend'
  | 'spend_correction'

/** One balance movement. `amount_micros` is signed: credits positive, debits negative. */
export type LedgerEntry = {
  id: string
  kind: LedgerKind
  amount_micros: number
  reason: string | null
  created_at: string
}

/** One model's price on the Jaah account, the org's markup included, in micro-USD per million tokens. */
export type JaahPrice = {
  family: string
  label: string
  input_micros: number
  output_micros: number
  cache_write_5m_micros: number
  cache_write_1h_micros: number
  cache_read_micros: number
}

/** The platform Bedrock account: `unset` = not configured, `broken` = configured but refusing. */
export type JaahPlatform = 'ok' | 'unset' | 'broken'

export type JaahMode = 'own' | 'jaah'

/** The org's auto-recharge. `threshold_micros`/`amount_micros` null = never set. `paused_until` (ISO) and
 *  `last_failure` (the server's sentence) are set after a failed charge; saving the settings retries at once. */
export type AutoRecharge = {
  enabled: boolean
  threshold_micros: number | null
  amount_micros: number | null
  paused_until: string | null
  last_failure: string | null
}

export type TopupStatus = 'pending' | 'paid' | 'failed'

/** One card charge into the balance. The balance rises only once Stripe confirms it (`paid`). */
export type JaahTopup = {
  id: number
  kind: 'topup' | 'auto_recharge'
  amount_micros: number
  status: TopupStatus
  failure_reason: string | null
  created_at: string
}

/** `GET /api/org/jaah-ai`. `mode`: which accounts the org's runs use now. `entries`: newest first, at most 20.
 *  `floor_micros` is the pause point — runs on the Jaah account pause once the balance reaches it.
 *  `has_default_card`: a saved default card, which (like a balance above the pause point) lets the org switch.
 *  `billing_connected` false = no card payments: no top-up, auto-recharge or refund (each would be a 409).
 *  `refundable_micros`: paid credit not yet spent, whole cents. `topups`: newest first, at most 5. */
export type JaahWallet = {
  available: boolean
  mode: JaahMode
  balance_micros: number
  floor_micros: number
  floor_state: FloorState
  low_threshold_micros: number
  entries: LedgerEntry[]
  platform: JaahPlatform
  has_default_card: boolean
  prices: JaahPrice[]
  billing_connected: boolean
  min_topup_micros: number
  max_topup_micros: number
  refundable_micros: number
  auto_recharge: AutoRecharge
  topups: JaahTopup[]
}

/** The operator's entry also names who made it: an operator's email, null = the system. */
export type OperatorLedgerEntry = LedgerEntry & { created_by_label: string | null }

/** `floor_overridden`/`markup_overridden` false = the value is the platform default. */
export type OperatorJaahWallet = Omit<JaahWallet, 'entries'> & {
  org_id: string
  org_name: string
  markup_bps: number
  floor_overridden: boolean
  markup_overridden: boolean
  free_micros: number
  paid_micros: number
  entries: OperatorLedgerEntry[]
}

/** A partial update. `null` on `floor_micros`/`markup_bps` returns that field to the platform default. */
export type OperatorJaahPatch = { available?: boolean; floor_micros?: number | null; markup_bps?: number | null }

/** `request_id`: minted once per dialog open, so a double-sent adjustment moves money once. */
export type JaahAdjustment = {
  direction: 'credit' | 'debit'
  amount_micros: number
  reason: string
  request_id: string
}

/** `request_id`: minted once per dialog open, so a double-sent top-up charges once. A non-null `client_secret`
 *  means Stripe wants 3-D Secure (`components/billing/payFlow.ts::authenticate`). */
export type TopupIn = { amount_micros: number; payment_method_id: string; request_id: string }
export type TopupOut = { topup: JaahTopup; client_secret: string | null; wallet: JaahWallet }
export type AutoRechargeIn = { enabled: boolean; threshold_micros: number; amount_micros: number }
export type RefundIn = { amount_micros: number; request_id: string }
/** `stopped`: why the rest was not refunded, when Stripe refused a later part of a multi-top-up refund. */
export type RefundOut = { refunded_micros: number; stopped: string | null; wallet: JaahWallet }

/** Query keys. The operator's extends the root, so invalidating the root refreshes every wallet read. */
export const JAAH_AI_QUERY = 'jaah-ai'
export const JAAH_WALLET_KEY = [JAAH_AI_QUERY, 'org'] as const
export const operatorJaahKey = (orgId: string) => [JAAH_AI_QUERY, 'operator', orgId] as const

/** The SSE entity every balance change emits (`id` = the org's id). */
export const TOKEN_WALLET_ENTITY = 'token_wallet'

const ROOT = '/api/org/jaah-ai'
const orgPath = (orgId: string) => `/api/billing/orgs/${encodeURIComponent(orgId)}/jaah-ai`

export const getJaahWallet = () => get<JaahWallet>(ROOT)
export const putLowThreshold = (low_threshold_micros: number) =>
  put<JaahWallet>(`${ROOT}/low-threshold`, { low_threshold_micros })
export const putJaahMode = (mode: JaahMode) => put<JaahWallet>(`${ROOT}/mode`, { mode })
/** 402 = declined (`detail` is Stripe's sentence); 422 refused; 404 unknown card; 409 billing not connected. */
export const postTopup = (body: TopupIn) => post<TopupOut>(`${ROOT}/topups`, body)
export const putAutoRecharge = (body: AutoRechargeIn) => put<JaahWallet>(`${ROOT}/auto-recharge`, body)
/** 422 = refused, nothing debited; 409 = a refund already in progress. */
export const postRefund = (body: RefundIn) => post<RefundOut>(`${ROOT}/refunds`, body)

export const getOperatorJaahWallet = (orgId: string) => get<OperatorJaahWallet>(orgPath(orgId))
export const putOperatorJaahWallet = (orgId: string, body: OperatorJaahPatch) =>
  put<OperatorJaahWallet>(orgPath(orgId), body)
export const postJaahAdjustment = (orgId: string, body: JaahAdjustment) =>
  post<OperatorJaahWallet>(`${orgPath(orgId)}/adjustments`, body)

/** One tenant's UTC day (`YYYY-MM-DD`): Jaah's metered cost (before markup) beside AWS's for its tag.
 *  `difference_micros` = Jaah − AWS; `over_tolerance` = beyond the operator's tolerance, which alerted. */
export type JaahReconciliation = {
  org_id: string
  org_name: string
  day: string
  jaah_cost_micros: number
  aws_cost_micros: number
  difference_micros: number
  tolerance_bps: number
  over_tolerance: boolean
}

export const JAAH_RECONCILIATIONS_KEY = [JAAH_AI_QUERY, 'reconciliations'] as const
/** The operator's Billing page (`finance:view`): newest day first, at most 200 rows. */
export const getJaahReconciliations = () => get<JaahReconciliation[]>('/api/billing/jaah-ai/reconciliations')

/* ── Money ─────────────────────────────────────────────────────────────────────────────────────────── */

const MICROS_PER_CENT = 10_000
const MINUS = '−'

/** Whole cents of `|micros|`, rounded half up on the absolute value (5_000 micros → 1 cent). Integer math. */
function absCents(micros: number): number {
  return Math.floor((Math.abs(micros) + MICROS_PER_CENT / 2) / MICROS_PER_CENT)
}

/** "$12.34", "−$20.00" (a real minus sign). An amount that rounds to zero is "$0.00", never "−$0.00". */
export function microsText(micros: number): string {
  const cents = absCents(micros)
  const text = moneyText(cents, 'usd')
  return micros < 0 && cents > 0 ? `${MINUS}${text}` : text
}

/** A ledger amount: "+$5.00", "−$1.20"; zero is "$0.00". */
export function signedMicrosText(micros: number): string {
  if (absCents(micros) === 0) return microsText(0)
  return micros > 0 ? `+${microsText(micros)}` : microsText(micros)
}

/** A typed dollar amount → micros: "12", "12.5", "$1,234.50", "-20" (a leading "-" or "−", for the pause
 *  point). At most two decimals. Null when it is not an amount. String math, so "0.29" is exactly 290_000. */
export function dollarsToMicros(text: string): number | null {
  const match = /^([-−])?\$?(\d+)(?:\.(\d{1,2}))?$/.exec(text.trim().replace(/,/g, ''))
  if (!match) return null
  const [, sign, whole, frac = ''] = match
  const micros = Number(whole) * 1_000_000 + Number(frac.padEnd(2, '0')) * MICROS_PER_CENT
  if (!Number.isSafeInteger(micros)) return null
  return sign && micros !== 0 ? -micros : micros
}

/** Micros → the text a dollars input starts from: "-20.00", "5.00" (ASCII minus, no "$", no grouping). */
export function microsToDollarsInput(micros: number): string {
  const cents = absCents(micros)
  const text = (cents / 100).toFixed(2)
  return micros < 0 && cents > 0 ? `-${text}` : text
}

/* ── Markup ────────────────────────────────────────────────────────────────────────────────────────── */

export const MARKUP_MAX_PERCENT = 1000

/** A typed percent (0–1000, at most two decimals) → basis points; null when out of range or not a number. */
export function percentToBps(text: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,2}))?%?$/.exec(text.trim())
  if (!match) return null
  const [, whole, frac = ''] = match
  const bps = Number(whole) * 100 + Number(frac.padEnd(2, '0'))
  return bps <= MARKUP_MAX_PERCENT * 100 ? bps : null
}

/** Basis points → the percent an input starts from: 1250 → "12.5", 0 → "0". */
export function bpsToPercentInput(bps: number): string {
  return String(bps / 100)
}

/* ── Words ─────────────────────────────────────────────────────────────────────────────────────────── */

export const ENTRY_LABEL: Record<LedgerKind, string> = {
  signup_credit: 'Signup credit',
  operator_credit: 'Credit from Jaah',
  operator_debit: 'Adjustment by Jaah',
  topup: 'Top-up',
  auto_recharge: 'Auto-recharge',
  refund: 'Refund',
  spend: 'Usage',
  spend_correction: 'Usage correction',
}

/** A kind this table has not met yet reads as a plain adjustment, never as its code. */
export function entryLabel(kind: string): string {
  return ENTRY_LABEL[kind as LedgerKind] ?? 'Adjustment'
}

/** The balance state's chip. `ok` is "Active"; the tenant card draws no chip for it. */
export const FLOOR_CHIP: Record<FloorState, { label: string; tone: 'success' | 'warning' | 'error' }> = {
  ok: { label: 'Active', tone: 'success' },
  low: { label: 'Low balance', tone: 'warning' },
  floor: { label: 'Paused', tone: 'error' },
}

/* ── The account switch ────────────────────────────────────────────────────────────────────────────── */

export const MODE_LABEL: Record<JaahMode, string> = {
  jaah: 'Use Jaah AI account (pay per token)',
  own: 'Use our own accounts',
}

/** Why the Jaah option cannot be chosen, or null: the plan's Jaah AI switch first, then the platform. */
export function jaahBlockedText(wallet: Pick<JaahWallet, 'available' | 'platform'>): string | null {
  if (!wallet.available) return 'Not on your plan'
  if (wallet.platform === 'unset') return 'Not available'
  if (wallet.platform === 'broken') return 'Jaah account unavailable'
  return null
}

/** Switching on needs a balance above the pause point or a saved card (signup and operator credit count). */
export function needsFunds(wallet: Pick<JaahWallet, 'balance_micros' | 'floor_micros' | 'has_default_card'>): boolean {
  return wallet.balance_micros <= wallet.floor_micros && !wallet.has_default_card
}

/** "Pauses at −$20.00": the pause point, as both surfaces say it. */
export const pausesAtText = (floorMicros: number) => `Pauses at ${microsText(floorMicros)}`

/* ── Funding: top-ups, auto-recharge, refunds ──────────────────────────────────────────────────────── */

/** The newest settled manual top-up when it failed, else null: a later success (or none yet) clears it.
 *  Auto-recharge failures are `AutoRecharge.last_failure`'s to say. */
export function lastTopupFailure(topups: JaahTopup[]): JaahTopup | null {
  const settled = topups.find(t => t.kind === 'topup' && t.status !== 'pending')
  return settled?.status === 'failed' ? settled : null
}

const sentence = (text: string) => text.trim().replace(/[.\s]+$/, '')

/** "Auto-recharge failed: <reason>. Paused until <date> — update your card or settings to retry now." Null
 *  when nothing failed. */
export function autoRechargeFailureText(auto: AutoRecharge): string | null {
  if (!auto.last_failure) return null
  const failed = `Auto-recharge failed: ${sentence(auto.last_failure)}.`
  if (!auto.paused_until) return failed
  return `${failed} Paused until ${shortDate(auto.paused_until, undefined, true)} — update your card or settings to retry now.`
}

/** "Top-up of $25.00 failed: Your card was declined." */
export function topupFailureText(topup: Pick<JaahTopup, 'amount_micros' | 'failure_reason'>): string {
  const head = `Top-up of ${microsText(topup.amount_micros)} failed`
  return topup.failure_reason ? `${head}: ${sentence(topup.failure_reason)}.` : `${head}.`
}

/** "$17.00 of paid credit is unspent and can be refunded to your card." */
export const refundableText = (micros: number) =>
  `${microsText(micros)} of paid credit is unspent and can be refunded to your card.`

/* ── Usage: the breakdown, the ledger over time, and the operator's cross-tenant summary ──────────── */

export type UsageGroupBy = 'task' | 'epic' | 'project' | 'user' | 'model'

/** `charge_micros` is what the tenant paid (markup included). */
export type UsageFigures = { charge_micros: number; tokens: number; calls: number }

/** One row of a grouping. `label` is a name, never an id; `task_id` is the task (or epic) it opens, null when
 *  there is none ("No task", "No epic", a deleted task, and every non-task grouping). */
export type UsageGroup = UsageFigures & { key: string; label: string; task_id: number | null }

/** `GET …/usage`: grouped server-side over one set, so every grouping of a range sums to `total`. `daily` is
 *  per calendar day in the zone asked for; `folded` = groups merged into the last "N others" row. */
export type JaahUsage = {
  group: UsageGroupBy
  start: string
  end: string
  total: UsageFigures
  groups: UsageGroup[]
  daily: (UsageFigures & { day: string })[]
  folded: number
}

/** A ledger row over a range. A day's usage is one row (`kind: 'spend'`, `id: 'spend:<day>'`, `calls` set). */
export type UsageLedgerRow = {
  id: string
  kind: LedgerKind
  amount_micros: number
  reason: string | null
  created_at: string
  calls: number | null
}

export type UsageLedgerPage = { entries: UsageLedgerRow[]; has_more: boolean }

/** The operator's row per tenant: spend (charged), cost (Bedrock's price), margin = spend − cost. */
export type TenantUsage = UsageFigures & {
  org_id: string
  org_name: string
  mode: JaahMode
  available: boolean
  balance_micros: number
  floor_state: FloorState
  cost_micros: number
  margin_micros: number
}

export type TenantsUsage = {
  start: string
  end: string
  charge_micros: number
  cost_micros: number
  margin_micros: number
  orgs: TenantUsage[]
}

/** Whose usage a view reads: the caller's own org, or one tenant on the operator's desk. */
export type UsageSource = { kind: 'tenant' } | { kind: 'operator'; orgId: string }

export type UsageRange = { from: string; to: string }

const usageRoot = (source: UsageSource) => (source.kind === 'tenant' ? ROOT : orgPath(source.orgId))
const zoneName = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

function query(params: Record<string, string | number>): string {
  return new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString()
}

/** Every usage read sits under this key, so one `token_wallet` event refreshes them all. */
export const usageKey = (source: UsageSource, ...rest: unknown[]) =>
  [JAAH_AI_QUERY, 'usage', source.kind === 'tenant' ? 'org' : source.orgId, ...rest] as const
export const JAAH_TENANTS_USAGE_KEY = [JAAH_AI_QUERY, 'usage', 'tenants'] as const

export const getJaahUsage = (source: UsageSource, range: UsageRange, group: UsageGroupBy) =>
  get<JaahUsage>(`${usageRoot(source)}/usage?${query({ ...range, group, tz: zoneName() })}`)
/** The page after `after` (the previous page's last row), newest first; null = the first page. */
export type LedgerCursor = { before: string; before_id: string } | null
export const getJaahUsageLedger = (source: UsageSource, range: UsageRange, after: LedgerCursor, limit: number) =>
  get<UsageLedgerPage>(`${usageRoot(source)}/ledger?${query({ ...range, tz: zoneName(), limit, ...(after ?? {}) })}`)
export const getJaahTenantsUsage = (range: UsageRange) =>
  get<TenantsUsage>(`/api/billing/jaah-ai/orgs?${query(range)}`)

/** `YYYY-MM-DD` as a local calendar day. */
export function localDay(day: string): Date {
  const [y, m, d] = day.split('-').map(Number)
  return new Date(y, m - 1, d)
}

/** A date's local calendar day as `YYYY-MM-DD`. */
export const dayKey = (date: Date) =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`

export type RangePreset = 'last7' | 'last30' | 'this_month' | 'last_month'

export const RANGE_PRESETS: readonly { id: RangePreset; label: string }[] = [
  { id: 'last7', label: 'Last 7 days' },
  { id: 'last30', label: 'Last 30 days' },
  { id: 'this_month', label: 'This month' },
  { id: 'last_month', label: 'Last month' },
]

/** A preset as local-midnight bounds, `to` exclusive. A range that runs to today ends at tomorrow's midnight, so
 *  the key stays put all day and a call made a second ago is inside it. */
export function presetRange(preset: RangePreset, now = new Date()): UsageRange {
  const day = (y: number, m: number, d: number) => new Date(y, m, d)
  const [y, m, d] = [now.getFullYear(), now.getMonth(), now.getDate()]
  const tomorrow = day(y, m, d + 1)
  switch (preset) {
    case 'last7': return { from: day(y, m, d - 6).toISOString(), to: tomorrow.toISOString() }
    case 'last30': return { from: day(y, m, d - 29).toISOString(), to: tomorrow.toISOString() }
    case 'this_month': return { from: day(y, m, 1).toISOString(), to: tomorrow.toISOString() }
    case 'last_month': return { from: day(y, m - 1, 1).toISOString(), to: day(y, m, 1).toISOString() }
  }
}

/** "1,234" below ten thousand, "12.3K" / "4.5M" above: a token count stays short in a phone row. */
export function tokensText(tokens: number): string {
  return tokens < 10_000 ? groupedCount(tokens) : compactCount(tokens)
}

/** "1 call", "12 calls". */
export const callsText = (calls: number) => `${groupedCount(calls)} ${calls === 1 ? 'call' : 'calls'}`

export const GROUP_LABEL: Record<UsageGroupBy, string> = {
  task: 'Task',
  epic: 'Epic',
  project: 'Project',
  user: 'User',
  model: 'Model',
}
