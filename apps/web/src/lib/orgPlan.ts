// The Subscription page's wire shapes and words (the API's org-plan routes). React-free.
// The plan catalogue, each plan's `offer` (the server's one decision of what to show), prices from Stripe via
// the server, the org's subscription, and its caps with their live usage. No purchase flow yet.

import { get, post } from '../api'
import { priceTagText } from './billing'
import { DAY_MS, shortDate } from './relativeTime'
import { countOf } from './humanize'
import type { MeOrgRef } from '../types'

/** A plan's monthly price. `amount` is in the currency's minor unit (cents). */
export type PlanPrice = { amount: number; currency: string; interval: string }

/** What a plan shows instead of (or as) its button: `selectable` = Choose/Current, `price` = its price,
 * `contact` = sales-assigned, `coming_soon` = not offered yet, `none` = assigned, never bought (the unpriced "(default)"). */
export type PlanOffer = 'selectable' | 'price' | 'contact' | 'coming_soon' | 'none'

export type OrgPlanOption = {
  key: string
  name: string
  blurb: string
  selectable: boolean
  offer: PlanOffer
  price: PlanPrice | null
  /** The yearly price, sold beside the monthly one; null = monthly only, or annual billing is off. */
  yearly: PlanPrice | null
  /** `price` (and `yearly`) is per agent: a purchase picks a count in [`min_agents`, `max_agents`] (null = no limit). */
  per_agent: boolean
  min_agents: number
  max_agents: number | null
  /** Buying it opens a free trial of this many days (once per org); null = none. */
  trial_days?: number | null
}

export type OrgSubscription = {
  state: string
  plan_key: string | null
  period_end: string | null
  /** The trial ends then, and the first charge lands; null when not trialing. */
  trial_end?: string | null
  scheduled: { plan_key: string | null; at: string | null } | null
}

/** One cap as the identity payload carries it. `limit` null = unlimited. The operator's Overrides tab (Limits section)
 * reads the fuller row (`pages/orgs/api.ts`), which extends this. */
export type OrgLimit = { kind: string; label: string; limit: number | null; usage: number }

export type OrgPlan = {
  plan: string
  plan_label: string
  /** The plan's end (null = never) and whether it has passed. Optional for fixtures. */
  plan_ends_at?: string | null
  plan_ended?: boolean
  /** A priced plan chosen at sign-up, awaiting checkout. Optional for fixtures. */
  pending_plan_key?: string | null
  pending_plan_name?: string | null
  /** The agent count chosen at sign-up for that pending plan; null = none (the plan minimum). Optional for fixtures. */
  pending_agents?: number | null
  /** A pending org's sign-up plan, applied when the org is approved. Optional for
   *  fixtures. */
  applied_plan_key?: string | null
  applied_plan_name?: string | null
  plan_chosen: boolean
  plans: OrgPlanOption[]
  limits: OrgLimit[]
  subscription: OrgSubscription | null
  billing_connected: boolean
  /** This org is a staging org billed with this parent (its name). Optional for fixtures. */
  billed_with?: string | null
  /** This org's staging org and its runner slots in use, counted in `limits`. Optional for fixtures. */
  staging?: { name: string; runner_slots: number } | null
}

/** The query key's head. */
export const ORG_PLAN_QUERY = 'org-plan'

export const getOrgPlan = () => get<OrgPlan>('/api/org/plan')
export const chooseOrgPlan = (plan: string) => post<OrgPlan>('/api/org/plan/choose', { plan })

/** A per-agent plan's agents as the sidebar throttle shows them, on a trial or the paid plan after it: the most agents
 *  wanted at once since the trial started (`peak`), those running now (`running`) and those bought (`signed_up`).
 *  `peak` null = no meter (not a per-agent plan), every other field null and `trialing` false. */
export type TrialAgents = {
  /** A live trial; `trial_end` is set only then. */
  trialing: boolean
  peak: number | null
  running: number | null
  signed_up: number | null
  trial_end: string | null
  /** The agent cap in force: the trial's lifted one, else the agents bought. */
  trial_cap: number | null
  /** The agent range a change may pick; `max_agents` null = no upper bound. */
  min_agents: number | null
  max_agents: number | null
  interval: 'month' | 'year' | null
  /** A staging org: the parent whose trial this is. */
  billed_with: string | null
}

/** The query key's head. */
export const TRIAL_AGENTS_QUERY = 'trial-agents'

export const getTrialAgents = () => get<TrialAgents>('/api/org/plan/trial-agents')

/** The plans a tenant can take, and only those: the org's own, a self-service (free) one, and a priced one with a
 *  price to show. A plan sold through sales (`contact`), Coming soon or assigned-only is not listed — nothing
 *  unavailable is shown (Products › Plans marks it for the operator); a single Contact us line is the way to ask. Only
 *  the org's own listed means Change has nothing to offer, so the Subscription page hides it. */
export function listedPlans(plan: OrgPlan): OrgPlanOption[] {
  return plan.plans.filter(o => o.key === plan.plan || o.selectable || (o.offer === 'price' && o.price !== null))
}

/** A cap in use and reached or passed ("1 / 1"): the Limits row to notice. An unused or unlimited cap never is. */
export function atLimit(limit: OrgLimit): boolean {
  return limit.limit !== null && limit.usage > 0 && limit.usage >= limit.limit
}

/** "3 / 5", or "3 / Unlimited" — unlimited is a word, never a 0. */
export function usageText(limit: OrgLimit): string {
  return `${limit.usage} / ${limit.limit === null ? 'Unlimited' : limit.limit}`
}

/** "$49/month", or "$49.50/month" — whole dollars drop the cents. "Free" for 0. */
export function priceText(price: PlanPrice): string {
  return price.amount === 0 ? 'Free' : `${priceTagText(price.amount, price.currency)}/${price.interval}`
}

/** "$20/agent/month · $200/agent/year": a plan's price with its per-agent unit and yearly price, when it has them.
 *  A monthly-only flat price reads as `priceText`. */
export function offerPriceText(price: PlanPrice, { perAgent = false, yearly = null }: { perAgent?: boolean; yearly?: PlanPrice | null } = {}): string {
  const one = (p: PlanPrice) => (perAgent && p.amount > 0 ? `${priceTagText(p.amount, p.currency)}/agent/${p.interval}` : priceText(p))
  return yearly ? `${one(price)} · ${one(yearly)}` : one(price)
}


/** A per-agent plan's range of agents it sells: "3–9", "3+" (no upper bound), or "3". */
export function agentRange(min: number, max: number | null): string {
  return max === null ? `${min}+` : min === max ? `${min}` : `${min}–${max}`
}

/** A per-agent plan's count is a choice (the agent stepper shows) only when its range holds more than one count. */
export const countPickable = (min: number, max: number | null): boolean => max === null || max > min

/** `agentRange` with its noun: "3–9 agents", "3+ agents", "1 agent". */
export function agentRangeText(min: number, max: number | null): string {
  return max === min ? agentsText(min) : `${agentRange(min, max)} agents`
}

/** "1 agent", "5 agents". */
export const agentsText = (n: number): string => countOf(n, 'agent')

/** The Contact us composer's prefill: the current plan and every cap, so nobody has to retype them. */
export const CONTACT_SUBJECT = 'Plan and limits'

/** What Contact us hands the New ticket page (`pages/support/NewTicketPage.tsx`'s prefill), from any surface that offers it. */
export function contactPrefill(plan: OrgPlan): { title: string; description: string } {
  return { title: CONTACT_SUBJECT, description: contactMessage(plan) }
}

export function contactMessage(plan: OrgPlan): string {
  const lines = plan.limits.map(limit => `- ${limit.label}: ${usageText(limit)}`)
  // Markdown (the New ticket page's editor): a blank line keeps the plan and the limits list as separate paragraphs.
  return [`Current plan: ${plan.plan_label}`, '', 'Limits (used / allowed):', ...lines, '', 'I would like to talk about: '].join('\n')
}

/** A plan held without Stripe, in words: "Current plan" and, when it has an end, "Ends <date>"; once past, "Plan ended".
 *  ONE home for the Billing list's Products row (`pages/org/BillingPage.tsx`) and the Subscription page's header, so the
 *  two say the same. */
export function heldPlanWords(ended: boolean, endsAt: string | null): { state: string; note: string | null } {
  if (ended) return { state: 'Plan ended', note: null }
  return { state: 'Current plan', note: endsAt ? `Ends ${shortDate(endsAt)}` : null }
}

// The same window as the server's Keep offer on a plan's included nodes (`plan_bundles.py::ENDING_AHEAD`).
const SOON_DAYS = 3

/* Which plan notice the org's state calls for, if any: `PlanEndedBanner`'s. Here, not with the banner, which the community edition's tree leaves out (`billing`). */
export function planNotice(org: MeOrgRef | null | undefined, now: Date): 'ended' | 'ending' | null {
  if (!org) return null
  if (org.plan_ended) return 'ended'
  const ends = org.plan_ends_at ? Date.parse(org.plan_ends_at) : NaN
  if (Number.isNaN(ends) || ends - now.getTime() > SOON_DAYS * DAY_MS || ends < now.getTime()) return null
  return 'ending'
}
