// The Billing page's wire shapes and words (the API's org billing routes). React-free, and free of
// Stripe.js: `lib/stripe.ts` is the one module that loads it, and only `components/billing/` imports that.
// The billing state's words and actions are `lib/billingState.ts`.

import { del, get, patch, post, put } from '../api'
import type { Tone } from '../ui/StatusChip'
import { humanize } from './humanize'

export type BillingAddress = {
  line1: string | null
  line2: string | null
  city: string | null
  state: string | null
  postal_code: string | null
  /** ISO 3166-1 alpha-2, upper case. */
  country: string | null
}

export type TaxId = { type: string; value: string }

export type BillingDetails = {
  name: string | null
  email: string | null
  address: BillingAddress | null
  tax_id: TaxId | null
}

/** An amount in the currency's minor unit (cents), with its lower-case ISO currency ("usd"). */
export type Money = { amount: number; currency: string }

/** The one word the Billing page is built around (`app/billing/status.py::tenant_state`, R10). `free` = no plan
 *  subscription: the org is on a plan it holds without Stripe (`lib/billingState.ts::isFree`). */
export type TenantState = 'free' | 'incomplete' | 'upgrade_pending' | 'active' | 'changing' | 'past_due' | 'ended'

/** The unpaid invoice Retry / Pay now / Complete payment acts on. */
export type OpenInvoice = { id: string; amount_due: number; currency: string; hosted_invoice_url: string | null }

export type Subscription = {
  state: TenantState
  /** Stripe's raw status (`active`, `past_due`, `incomplete`, `unpaid`, `canceled`, …) — never shown. */
  status: string
  plan_key: string | null
  plan_name: string | null
  /** Per agent on a per-agent plan; `interval` is `month` or `year`. */
  price: { amount: number; currency: string; interval: string } | null
  /** A per-agent plan's purchase: the agents bought and the plan's range (`max` null = no limit); null otherwise. */
  agents: { count: number; min: number; max: number | null } | null
  /** The interval the org may switch to (`POST …/interval`); null = none. */
  switch_interval: BillingInterval | null
  /** The current period's end: the next renewal date. */
  period_end: string | null
  cancel_at_period_end: boolean
  /** A future change. `plan_key` null = cancelling at period end; `plan_name` names the plan the org moves to. */
  scheduled: { plan_key: string | null; plan_name: string; at: string } | null
  ended_at: string | null
  open_invoice: OpenInvoice | null
}

/** A self-serve first purchase still under way. */
export type Checkout = { state: 'started' | 'card_saved' | 'subscribing'; plan_key: string; plan_name: string; attempt: number }

/** `{available: false}` = Stripe could not be asked right now — a calm line, never an error. */
export type NextCharge = { available: true; at: string; amount: number; currency: string } | { available: false }

/** A dunning or card-expiry notice (`app/billing/notices.py::wire`). `text` is the server's whole sentence,
 *  rendered verbatim — the web never composes it. `kind` is opaque here. `banner` = also shown as a page
 *  banner. `action.href` is a console path ("/org/billing#payment-methods") or Stripe's https page; anything
 *  else is no action (`noticeAction`). */
export type BillingNotice = {
  id: string
  kind: string
  banner: boolean
  severity: 'warning' | 'info'
  text: string
  action: { label: string; href: string } | null
  created_at: string
}

/** The org's hosting subscription (`GET /api/org/billing`): its hostings bill on it, apart from the plan's, which may be
 *  free or trialing. `collection` is Stripe's `charge_automatically` or `send_invoice`. */
export type HostingSubscription = {
  status: string
  period_end: string | null
  collection: string | null
  open_invoice: OpenInvoice | null
}

/** `GET /api/org/billing`. `subscription` null = no plan subscription (state `free`); `next_charge` null = nothing will be
 *  charged. `hosting_subscription` null = no hosting billed; `next_hosting_charge` is its own next charge. `notices`:
 *  unresolved, newest first. */
export type OrgBilling = {
  connected: boolean
  publishable_key: string | null
  details: BillingDetails | null
  subscription: Subscription | null
  checkout: Checkout | null
  next_charge: NextCharge | null
  hosting_subscription?: HostingSubscription | null
  next_hosting_charge?: NextCharge | null
  notices: BillingNotice[]
}

export type BillingInterval = 'month' | 'year'

/** `POST …/preview`: what Confirm would charge. `kind` is the server's call, never inferred from prices here.
 *  `agents`: more agents — `due_now` is charged now (prorated), `next_charge.amount` the per-period total at the new
 *  count; fewer — nothing due now, `next_charge` the next invoice with the credit. `interval`: `due_now` is charged
 *  now, `next_charge.amount` the new per-period amount. */
export type Preview = {
  preview_id: string
  kind: 'subscribe' | 'upgrade' | 'downgrade' | 'agents' | 'interval'
  due_now: Money
  next_charge: { at: string; amount: number; currency: string }
  /** A free trial this purchase opens, in days: `due_now` is 0 and `next_charge` is the first charge, at its end. */
  trial_days?: number | null
  lines: { description: string; amount: number; currency: string }[]
}

/** `cancel`/`undo`/`checkout/abandon`: the state after. No payment, so no secret. */
export type BillingState = {
  state: TenantState
  subscription: Subscription | null
  checkout: Checkout | null
}

/** `subscribe`/`change`: as `BillingState`, plus a non-null `client_secret` when 3-D Secure is owed. */
export type BillingChange = BillingState & { client_secret: string | null }

export type InvoiceStatus = 'draft' | 'open' | 'paid' | 'void' | 'uncollectible' | 'refunded' | 'partially_refunded'

export type Invoice = {
  id: string
  number: string | null
  created: string | null
  amount_due: number
  amount_paid: number
  amount_refunded: number
  currency: string
  status: InvoiceStatus
  hosted_invoice_url: string | null
  invoice_pdf: string | null
  credit_notes: { id: string; number: string | null; amount: number; pdf: string | null }[]
}

/** One invoice's lines, read through the console (`GET …/invoices/{id}/lines`). Amounts in cents. */
export type InvoiceLines = {
  currency: string
  lines: { description: string; amount_cents: number; quantity: number | null; period_start: string | null; period_end: string | null }[]
  subtotal_cents: number
  total_cents: number
}

export type PaymentMethodStatus = 'ok' | 'expires_soon' | 'expired' | 'last_charge_failed'

export type PaymentMethod = {
  id: string
  /** Stripe's payment method type: `card`, `us_bank_account`, `sepa_debit`, `link`, … */
  type: string
  brand: string | null
  wallet: string | null
  last4: string | null
  exp_month: number | null
  exp_year: number | null
  is_default: boolean
  status: PaymentMethodStatus
}

/** `DELETE …/{id}` with or without `dry_run`. `refused` is only ever set on a dry run (a real refusal is a 409). */
export type DeletePlan = {
  refused: string | null
  promotes: PaymentMethod | null
  payment_methods: PaymentMethod[]
}

export type DetailsIn = { name: string; email: string; address: Partial<BillingAddress> | null; tax_id: TaxId | null }
export type PaymentMethodPatch = { exp_month?: number; exp_year?: number; name?: string; address?: Partial<BillingAddress> }

/** Query keys. Each list's key extends the frame's, so invalidating the frame refreshes them all. */
export const ORG_BILLING_QUERY = 'org-billing'
export const PAYMENT_METHODS_KEY = [ORG_BILLING_QUERY, 'payment-methods'] as const
export const INVOICES_KEY = [ORG_BILLING_QUERY, 'invoices'] as const
export const BILLING_NOTICES_KEY = [ORG_BILLING_QUERY, 'notices'] as const

const ROOT = '/api/org/billing'
const pmPath = (id: string) => `${ROOT}/payment-methods/${encodeURIComponent(id)}`

export const getOrgBilling = () => get<OrgBilling>(ROOT)
export const putBillingDetails = (body: DetailsIn) => put<BillingDetails>(`${ROOT}/details`, body)
export const getPaymentMethods = () => get<PaymentMethod[]>(`${ROOT}/payment-methods`)
/** The SetupIntent's client secret: held by the add-card dialog for its lifetime only, never logged or stored. */
export const createSetupIntent = () => post<{ client_secret: string }>(`${ROOT}/setup-intents`)
export const makeDefault = (id: string) => post<PaymentMethod[]>(`${pmPath(id)}/default`)
export const updatePaymentMethod = (id: string, body: PaymentMethodPatch) => patch<PaymentMethod>(pmPath(id), body)
export const planDelete = (id: string) => del<DeletePlan>(`${pmPath(id)}?dry_run=1`)
export const deletePaymentMethod = (id: string) => del<DeletePlan>(pmPath(id))
export const syncBilling = () => post<PaymentMethod[]>(`${ROOT}/sync`)

/* Purchases. A body carries a plan key (and the preview it confirms, the agents, the interval) only — never an
 * amount: the server prices. `agents`/`interval` are sent only when set. */
export type PurchaseOptions = { agents?: number; interval?: BillingInterval }
const extras = ({ agents, interval }: PurchaseOptions = {}) => ({
  ...(agents !== undefined ? { agents } : {}),
  ...(interval !== undefined ? { interval } : {}),
})
export const previewPlan = (plan_key: string, opts?: PurchaseOptions) => post<Preview>(`${ROOT}/preview`, { plan_key, ...extras(opts) })
export const subscribe = (plan_key: string, opts?: PurchaseOptions) =>
  post<BillingChange>(`${ROOT}/subscribe`, { plan_key, ...extras(opts) })
export const changePlan = (plan_key: string, preview_id?: string, opts?: { agents?: number }) =>
  post<BillingChange>(`${ROOT}/change`, { plan_key, ...(preview_id ? { preview_id } : {}), ...extras(opts) })
/** A per-agent plan's agent count. More agents are charged now, prorated (`client_secret` set when 3-D Secure is owed);
 *  fewer are a credit on the next invoice. */
export const previewAgents = (agents: number) => post<Preview>(`${ROOT}/agents/preview`, { agents })
export const changeAgents = (agents: number, preview_id: string) => post<BillingChange>(`${ROOT}/agents`, { agents, preview_id })
/** Monthly ↔ yearly: charged now; `client_secret` set when 3-D Secure is owed. */
export const previewInterval = (interval: BillingInterval) => post<Preview>(`${ROOT}/interval/preview`, { interval })
export const changeInterval = (interval: BillingInterval, preview_id: string) =>
  post<BillingChange>(`${ROOT}/interval`, { interval, preview_id })
export const cancelPlan = () => post<BillingState>(`${ROOT}/cancel`)
export const undoChange = () => post<BillingState>(`${ROOT}/undo`)
export const abandonCheckout = () => post<BillingState>(`${ROOT}/checkout/abandon`)
export const getInvoices = () => get<Invoice[]>(`${ROOT}/invoices`)
export const getInvoiceLines = (id: string) => get<InvoiceLines>(`${ROOT}/invoices/${encodeURIComponent(id)}/lines`)
/** Owner/Admin only: unresolved notices, newest first. */
export const getBillingNotices = () => get<BillingNotice[]>(`${ROOT}/notices`)
export const payInvoice = (id: string) =>
  post<{ status: string; client_secret: string | null }>(`${ROOT}/invoices/${encodeURIComponent(id)}/pay`)

/** Where a notice's action goes: a console path (`internal`, for the router) or Stripe's https page
 *  (`external`, a new tab). Any other href — `javascript:`, `http:`, another host, `//host`, `/\host` — is
 *  no action. */
export function noticeAction(
  action: BillingNotice['action'],
): { label: string; href: string; external: boolean } | null {
  const href = action?.href
  if (!href || !action.label) return null
  // A browser strips tabs and newlines and reads `\` as `/`, so `/\t/evil.example` would be another host.
  if (/[\u0000-\u001f\\]/.test(href)) return null
  // `//` is protocol-relative to a browser: another host.
  if (/^\/(?!\/)/.test(href)) return { ...action, external: false }
  // External = Stripe's hosted invoice page only (the server admits nothing else: `notices.hosted_invoice_link`).
  try {
    const url = new URL(href)
    const host = url.hostname.toLowerCase()
    if (url.protocol === 'https:' && !url.username && !url.password && (host === 'stripe.com' || host.endsWith('.stripe.com')))
      return { ...action, external: true }
  } catch {
    return null
  }
  return null
}

/* ── Money and dates ─────────────────────────────────────────────────────────────────────────────── */

/** "$12.34", "$49.00": an amount always shows its cents (a price tag drops them — `priceTagText`). */
export function moneyText(amount: number, currency: string): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency.toUpperCase() }).format(amount / 100)
}

/** "$49", "$49.50", "$12,345": a price tag drops whole-dollar cents. */
export function priceTagText(cents: number, currency: string): string {
  const digits = cents % 100 === 0 ? 0 : 2
  const opts = { style: 'currency', currency: currency.toUpperCase(), minimumFractionDigits: digits, maximumFractionDigits: digits } as const
  return new Intl.NumberFormat('en-US', opts).format(cents / 100)
}

/** "49" or "49.50" — the field's text for a stored amount. */
export function dollarsText(cents: number): string {
  return cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2)
}

/** The typed dollars as integer cents; null when it is not a money amount. */
export function centsFromDollars(text: string): number | null {
  const t = text.trim().replace(/,/g, '')
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null
  return Math.round(Number(t) * 100)
}

/** The server's price bounds (`billing_plans.MAX_AMOUNT`); Stripe's minimum charge is 50 cents. */
export const MIN_CENTS = 50
export const MAX_CENTS = 10_000_000

/** Why a price in cents is out of bounds, or '' when it is not. `allowFree` admits 0. */
export function priceError(cents: number, { allowFree = false } = {}): string {
  if (allowFree && cents === 0) return ''
  if (cents < MIN_CENTS) return allowFree ? 'Free (0) or at least $0.50.' : 'At least $0.50.'
  return cents > MAX_CENTS ? 'At most $100,000.' : ''
}

/** A free trial's time left, from its end: "12 days left", "1 day left", or "Ended" once past (days round up). */
export function trialLeftText(endIso: string, now: Date = new Date()): string {
  const days = Math.ceil((Date.parse(endIso) - now.getTime()) / 86_400_000)
  return days <= 0 ? 'Ended' : `${days} ${days === 1 ? 'day' : 'days'} left`
}

/** "Nov 3", or "Nov 3, 2027" outside the current year; `withTime` adds the clock ("Nov 3, 2:05 PM"). Local
 *  time, en-US. An unparseable date reads as itself. The console's ONE short date (the Compliance pages re-export it). */
export function shortDate(iso: string, now: Date = new Date(), withTime = false): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const sameYear = date.getFullYear() === now.getFullYear()
  return date.toLocaleString('en-US', {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }), ...(withTime ? { hour: 'numeric', minute: '2-digit' } : {}),
  })
}

/** Invoice status → its chip. */
export const INVOICE_CHIP: Record<InvoiceStatus, { label: string; tone: Tone }> = {
  paid: { label: 'Paid', tone: 'success' },
  open: { label: 'Open', tone: 'warning' },
  void: { label: 'Void', tone: 'neutral' },
  uncollectible: { label: 'Uncollectible', tone: 'error' },
  refunded: { label: 'Refunded', tone: 'info' },
  partially_refunded: { label: 'Partially refunded', tone: 'info' },
  draft: { label: 'Draft', tone: 'neutral' },
}

/* ── Words ─────────────────────────────────────────────────────────────────────────────────────────── */

/** Card brand → [name, the short mark its tile draws, the tile's accent]. Stripe's brand codes. */
const BRANDS: Record<string, readonly [string, string, string]> = {
  visa: ['Visa', 'VISA', '#1434cb'],
  mastercard: ['Mastercard', 'MC', '#eb001b'],
  amex: ['American Express', 'AMEX', '#2e77bc'],
  discover: ['Discover', 'DISC', '#ff6000'],
  diners: ['Diners Club', 'DC', '#0079be'],
  jcb: ['JCB', 'JCB', '#0b8a3e'],
  unionpay: ['UnionPay', 'UP', '#e21836'],
  cartes_bancaires: ['Cartes Bancaires', 'CB', '#1d4f91'],
  eftpos_au: ['eftpos', 'EFT', '#6b7280'],
  interac: ['Interac', 'INT', '#f5a623'],
}

/** A non-card method type → [name, mark]. */
const TYPES: Record<string, readonly [string, string]> = {
  us_bank_account: ['Bank account', 'BANK'],
  sepa_debit: ['SEPA Debit', 'SEPA'],
  bacs_debit: ['Bacs Debit', 'BACS'],
  au_becs_debit: ['BECS Debit', 'BECS'],
  acss_debit: ['Pre-authorized debit', 'PAD'],
  link: ['Link', 'LINK'],
  paypal: ['PayPal', 'PP'],
}

const WALLETS: Record<string, string> = {
  apple_pay: 'Apple Pay',
  google_pay: 'Google Pay',
  link: 'Link',
  samsung_pay: 'Samsung Pay',
  amex_express_checkout: 'Amex Express Checkout',
  masterpass: 'Masterpass',
  visa_checkout: 'Visa Checkout',
}

/** The brand tile's content: full name (its accessible name), short mark, and an accent or null. */
export function brandMark(pm: Pick<PaymentMethod, 'type' | 'brand'>): { name: string; mark: string; accent: string | null } {
  if (pm.type === 'card') {
    const known = pm.brand ? BRANDS[pm.brand] : undefined
    if (known) return { name: known[0], mark: known[1], accent: known[2] }
    return { name: pm.brand && pm.brand !== 'unknown' ? humanize(pm.brand) : 'Card', mark: 'CARD', accent: null }
  }
  const known = TYPES[pm.type]
  if (known) return { name: known[0], mark: known[1], accent: null }
  const name = humanize(pm.type)
  return { name, mark: name.slice(0, 4).toUpperCase(), accent: null }
}

export function walletName(wallet: string | null): string | null {
  return wallet ? WALLETS[wallet] ?? humanize(wallet) : null
}

/** "•••• 4242" — or nothing, for a method Stripe gave no last four for. */
export function maskedNumber(pm: PaymentMethod): string | null {
  return pm.last4 ? `•••• ${pm.last4}` : null
}

/** "Visa •••• 4242": how a confirmation names a method. */
export function methodLabel(pm: PaymentMethod): string {
  const number = maskedNumber(pm)
  return number ? `${brandMark(pm).name} ${number}` : brandMark(pm).name
}

/** "12/30"; null for a method with no expiry. */
export function expiryText(pm: Pick<PaymentMethod, 'exp_month' | 'exp_year'>): string | null {
  if (pm.exp_month == null || pm.exp_year == null) return null
  return `${String(pm.exp_month).padStart(2, '0')}/${String(pm.exp_year % 100).padStart(2, '0')}`
}

/** A status worth a chip; `ok` draws none. */
export const STATUS_CHIP: Record<Exclude<PaymentMethodStatus, 'ok'>, { label: string; tone: 'warning' | 'error' }> = {
  expires_soon: { label: 'Expires soon', tone: 'warning' },
  expired: { label: 'Expired', tone: 'error' },
  last_charge_failed: { label: 'Last charge failed', tone: 'error' },
}

/** Common Stripe tax id types for the details form; a stored type outside this list is still offered. */
export const TAX_ID_TYPES: readonly { code: string; label: string }[] = [
  { code: 'eu_vat', label: 'EU VAT' },
  { code: 'gb_vat', label: 'UK VAT' },
  { code: 'us_ein', label: 'US EIN' },
  { code: 'ca_bn', label: 'Canada BN' },
  { code: 'ca_gst_hst', label: 'Canada GST/HST' },
  { code: 'au_abn', label: 'Australia ABN' },
  { code: 'nz_gst', label: 'New Zealand GST' },
  { code: 'ch_vat', label: 'Switzerland VAT' },
  { code: 'no_vat', label: 'Norway VAT' },
  { code: 'in_gst', label: 'India GST' },
  { code: 'jp_cn', label: 'Japan CN' },
  { code: 'sg_gst', label: 'Singapore GST' },
  { code: 'za_vat', label: 'South Africa VAT' },
  { code: 'br_cnpj', label: 'Brazil CNPJ' },
  { code: 'mx_rfc', label: 'Mexico RFC' },
]

export function taxIdLabel(type: string): string {
  return TAX_ID_TYPES.find(t => t.code === type)?.label ?? type
}

/** The address's display lines, blanks dropped: street, street 2, "City, State 94107", country. */
export function addressLines(address: BillingAddress | null): string[] {
  if (!address) return []
  const locality = [[address.city, address.state].filter(Boolean).join(', '), address.postal_code]
    .filter(Boolean)
    .join(' ')
  return [address.line1, address.line2, locality, address.country].filter((line): line is string => !!line)
}

/** The non-blank fields of an address form, trimmed; null when every one is blank. */
export function compactAddress(form: Record<keyof BillingAddress, string>): Partial<BillingAddress> | null {
  const out: Partial<BillingAddress> = {}
  for (const key of Object.keys(form) as (keyof BillingAddress)[]) {
    const value = form[key].trim()
    if (value) out[key] = key === 'country' ? value.toUpperCase() : value
  }
  return Object.keys(out).length ? out : null
}
