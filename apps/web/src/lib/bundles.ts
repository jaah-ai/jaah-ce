import { get, post } from '../api'
import type { OrderCreated } from '../components/addNode/api'
import type { Tone } from '../ui/StatusChip'

/* ── A bundled product's status ──
The single home for how the console reads a bundle copy's `status` (`app/plan_bundles.py` STATUS, §4.8; the
operator's `GET /api/orgs/{id}/bundles` and the tenant's own list share the vocabulary): its word, its tone, and
whether Retry (`POST …/bundles/{bid}/retry`) is legal. Never render the raw status; an unknown one reads as
`UNKNOWN_LABEL`. */

/** One copy of a node the org's plan includes (`routes/orgs.py::BundleOut`, Architecture §4.8). Never render
 * `status` raw: read it through `bundleWord`. `note` is the sentence a surface shows — the operator's detail on
 * `GET /api/orgs/{id}/bundles`, the tenant-safe one on `GET /api/org/bundles`; `stops_at` when a copy the plan no
 * longer includes stops; `keep` whether it may be kept as a paid hosting now, and its monthly price. */
export type BundleStatus = 'provisioning' | 'failed' | 'ready' | 'removed' | 'stopping' | 'stopped' | 'kept' | 'terminated'
export type OrgBundle = {
  id: string
  kind: string
  label: string
  ordinal: number
  status: BundleStatus | string
  note: string | null
  node_id: string | null
  hosting_id: string | null
  stops_at: string | null
  keep: { offered: boolean; monthly_cents: number | null }
}

/** The viewer's own tenant org's bundles (any member; an operator-plane caller is answered 404). */
export const OWN_BUNDLES_KEY = ['org-bundles'] as const
export const listOwnBundles = () => get<OrgBundle[]>('/api/org/bundles')

/** Keep a copy the plan no longer includes as a paid hosting: the same node, paid now (3-D Secure through the
 *  order's confirm). An Org Owner/Admin's; answered in the Add node order's shape. */
export const keepBundle = (bundleId: string, paymentMethodId?: string) =>
  post<OrderCreated>(`/api/bundles/${encodeURIComponent(bundleId)}/keep`, paymentMethodId ? { payment_method_id: paymentMethodId } : {})

/** Re-ask every 10s while a copy is being prepared, so a node placed meanwhile replaces its placeholder; never
 *  otherwise. A react-query `refetchInterval`. */
export const PREPARING_POLL_MS = 10_000
export function preparingPoll(query: { state: { data?: OrgBundle[] } }): number | false {
  return query.state.data?.some(b => b.status === 'provisioning') ? PREPARING_POLL_MS : false
}

/** The copies the plan includes now: held, not released. */
export const INCLUDED: readonly string[] = ['provisioning', 'failed', 'ready', 'removed']

/** A status this module has no word for — a server newer than the console. */
export const UNKNOWN_LABEL = 'Status unknown'

type Word = { label: string; tone: Tone }

const WORDS: Record<string, Word> = {
  provisioning: { label: 'Preparing…', tone: 'info' },
  failed: { label: 'Failed', tone: 'error' },
  ready: { label: 'Ready', tone: 'success' },
  removed: { label: 'Removed', tone: 'neutral' },
  stopping: { label: 'Stopping', tone: 'warning' },
  stopped: { label: 'Stopped', tone: 'neutral' },
  kept: { label: 'Kept', tone: 'success' },
  terminated: { label: 'Ended', tone: 'neutral' },
}
const UNKNOWN: Word = { label: UNKNOWN_LABEL, tone: 'neutral' }

export function bundleWord(status: string): Word {
  return WORDS[status] ?? UNKNOWN
}

/** The statuses the server will retry (`plan_bundles.py::RETRYABLE`); any other is a 409. */
export const RETRYABLE: readonly string[] = ['failed', 'removed']

export function bundleCanRetry(status: string): boolean {
  return RETRYABLE.includes(status)
}
