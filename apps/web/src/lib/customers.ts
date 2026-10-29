// The operator's Customers roster: every org with its status, plan, MRR and billing state at a glance
// (`GET /api/customers`). React-free. Amounts are integer minor units with their lower-case currency;
// `lib/billing.ts` formats them. Server-paged and sorted: one page per read,
// `hooks/useListPage`'s page/per/sort/dir appended to the filters.

import { del, get, patch, post } from '../api'
import { setPageParams, type ListPageState, type Page } from '../hooks/useListPage'
import type { DeliveryStatus } from '../pages/settings/emailDelivery/api'
import type { Tone } from '../ui/StatusChip'

/** A customer page's tabs (`pages/customers/customerTabs.ts` holds their labels and the retired addresses). */
export type CustomerTab = 'overview' | 'access' | 'products' | 'billing' | 'overrides' | 'settings' | 'security' | 'history'

export const DEFAULT_CUSTOMER_TAB: CustomerTab = 'overview'

/** A customer tab, plus any further query the link carries. */
export type CustomerTarget = { tab: CustomerTab; query?: Readonly<Record<string, string>> }

/** `/customers/:id` on `target`'s tab; the default tab is the bare path, as `useUrlTab` writes it. */
export function customerUrl(id: string, target: CustomerTarget = { tab: DEFAULT_CUSTOMER_TAB }): string {
  const params = new URLSearchParams()
  if (target.tab !== DEFAULT_CUSTOMER_TAB) params.set('tab', target.tab)
  for (const [key, value] of Object.entries(target.query ?? {})) params.set(key, value)
  const query = params.toString()
  return `/customers/${encodeURIComponent(id)}${query ? `?${query}` : ''}`
}

/** `signup`: an unfinished sign-up — an email with no org yet, or an org still in its draft. */
export type CustomerStatus = 'signup' | 'pending' | 'enabled' | 'disabled' | 'rejected' | 'deleted'
/** How far the sign-up got: the step it is on, `submitted` awaiting approval, `approved` done. */
export type CustomerStage = 'verify_email' | 'password' | 'profile' | 'plan' | 'billing_details' | 'card' | 'submitted' | 'approved'
export type CustomerBillingState = 'past_due' | 'paying' | 'comped' | 'included' | 'none'

/** An email-only sign-up's own facts (`kind: 'email_signup'` rows only). */
export type SignupFacts = {
  email: string
  started_at: string
  code_sent_at: string | null
  sends: number
  verified_at: string | null
  /** The latest code email to the address; null when none is on the delivery log. */
  last_email: { status: DeliveryStatus; detail: string | null; at: string } | null
}

export type CustomerRow = {
  /** An email sign-up's own id on `email_signup` rows: it has no org yet, so no customer page. */
  id: string
  /** `email_signup`: name = the email, slug empty, no plan, every count 0, status `signup`. */
  kind: 'org' | 'email_signup'
  stage: CustomerStage
  signup?: SignupFacts | null
  name: string
  slug: string
  status: CustomerStatus
  is_operator_org: boolean
  /** null on the operator's own org (it is on no plan). `ends_at` null = never ends. */
  plan: { key: string; name: string; ends_at: string | null } | null
  /** null without `finance:view`, while Stripe is not connected, and on the operator's own org. */
  mrr: { amount: number; currency: string } | null
  billing_state: CustomerBillingState | null
  /** What a billed org pays for: the plan its subscription bills and its billed hostings ("Individual plan · 1 hosting"). */
  paid_for?: string | null
  /** The default card is expiring, expired or its last charge failed; null as `mrr` is. */
  card_issue: boolean | null
  /** The plan subscription is on a price the plan no longer sells; null as `mrr` is. */
  old_price: boolean | null
  /** When the plan subscription's free trial ends, while it runs; null otherwise and as `mrr` is. */
  trial_end?: string | null
  products: number
  users: number
  /** Audit events (reads and writes) in the last 24 hours / 7 days. */
  activity_24h: number
  activity_7d: number
  pending: boolean
  hipaa: boolean
  rejection_reason: string | null
}

/** One page of the roster, plus the billing connection it was read under and the WHOLE filtered set's MRR, one
 *  entry per currency (summed by the server, so it never reads as a page's sum); null as each row's `mrr` is. */
export type Customers = Page<CustomerRow> & {
  connected: boolean
  livemode: boolean | null
  mrr_total: { amount: number; currency: string }[] | null
  /** Under `status=signup` only: the unfinished sign-ups; `verified` = verified, no org yet. */
  signups?: { total: number; unverified: number; verified: number } | null
}

/** The roster's sortable columns (`routes/customer_roster.py::_SORTS`); none = the roster's own order (the
 *  operator's org, then sign-ups awaiting approval, then by name). */
export const CUSTOMER_SORTS = ['name', 'status', 'plan', 'mrr', 'products', 'users', 'activity_24h', 'activity_7d'] as const

/** The list's filters, as the URL carries them (empty = no filter). */
export type CustomerFilters = { q: string; status: string; plan: string; billing: string; attention: string; hostings: string }

export const CUSTOMER_FILTER_KEYS = ['q', 'status', 'plan', 'billing', 'attention', 'hostings'] as const

export const CUSTOMERS_QUERY = 'customers'

export function getCustomers(filters: CustomerFilters, page: ListPageState): Promise<Customers> {
  const params = new URLSearchParams()
  for (const key of CUSTOMER_FILTER_KEYS) {
    const value = filters[key].trim()
    if (value) params.set(key, value)
  }
  setPageParams(params, page)
  return get<Customers>(`/api/customers?${params.toString()}`)
}

/** Mails an email sign-up a fresh code; 404 once the row is used, 429 `resend_cooldown` inside the wait. */
export const resendSignupCode = (id: string) =>
  post<{ email: string; resend_after: number }>(`/api/operator/signups/${encodeURIComponent(id)}/resend`)

/** Verifies an email sign-up for the applicant and creates its draft org (idempotent): the customer to open. */
export const verifySignupEmail = (id: string) =>
  post<{ org_id: string }>(`/api/operator/signups/${encodeURIComponent(id)}/verify`)

/** Deletes an email sign-up with no org yet; 409 once it has a draft (delete that customer instead). */
export const deleteSignup = (id: string) => del(`/api/operator/signups/${encodeURIComponent(id)}`)

/** A pending customer's sign-up answers and plan, as the operator edits them on Overview
 *  (`routes/customer_roster.py::SignupOut`). */
export type CustomerSignup = {
  account_type: 'individual' | 'organization' | null
  first_name: string | null
  last_name: string | null
  org_name: string
  country: string | null
  occupation: string | null
  intended_use: string | null
  plan_key: string | null
  stage: CustomerStage
  /** Unsubmitted: the applicant (or an operator's approve) has not signed up yet. */
  draft: boolean
}

/** The fields an edit may send; only the ones sent change. */
export type CustomerSignupEdit = Partial<Omit<CustomerSignup, 'stage' | 'draft'>>

export const customerSignupKey = (orgId: string) => ['customer-signup', orgId] as const

export const getCustomerSignup = (orgId: string) =>
  get<CustomerSignup>(`/api/customers/${encodeURIComponent(orgId)}/signup`)

export const editCustomerSignup = (orgId: string, edit: CustomerSignupEdit) =>
  patch<CustomerSignup>(`/api/customers/${encodeURIComponent(orgId)}/signup`, edit)

export const STATUS_CHIP: Record<CustomerStatus, { label: string; tone: Tone }> = {
  signup: { label: 'Sign-up', tone: 'info' },
  pending: { label: 'Pending', tone: 'warning' },
  enabled: { label: 'Enabled', tone: 'success' },
  disabled: { label: 'Disabled', tone: 'neutral' },
  rejected: { label: 'Rejected', tone: 'error' },
  /* Listed only under `status=deleted`; the unfiltered roster omits it. */
  deleted: { label: 'Deleted', tone: 'error' },
}

export const STAGE_LABEL: Record<CustomerStage, string> = {
  verify_email: 'Verify email',
  password: 'Password',
  profile: 'Profile',
  plan: 'Plan',
  billing_details: 'Billing details',
  card: 'Card',
  submitted: 'Submitted',
  approved: 'Approved',
}

export const BILLING_CHIP: Record<CustomerBillingState, { label: string; tone: Tone }> = {
  past_due: { label: 'Past due', tone: 'error' },
  paying: { label: 'Paying', tone: 'success' },
  comped: { label: 'Comped', tone: 'info' },
  included: { label: 'Included', tone: 'info' },
  none: { label: 'None', tone: 'neutral' },
}

export const STATUS_OPTIONS = (Object.keys(STATUS_CHIP) as CustomerStatus[]).map(value => ({ value, label: STATUS_CHIP[value].label }))
export const ATTENTION_OPTIONS = [{ value: 'card', label: 'Card issues' }, { value: 'old_price', label: 'Old price' }]
export const BILLING_OPTIONS = (Object.keys(BILLING_CHIP) as CustomerBillingState[]).map(value => ({ value, label: BILLING_CHIP[value].label }))
