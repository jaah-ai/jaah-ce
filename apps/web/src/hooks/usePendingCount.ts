import { type QueryKey, useQuery } from '@tanstack/react-query'
import { PENDING_ORG_COUNT_KEY, getPendingOrgCount } from '../lib/orgs'
import { isEnabled } from '../lib/editionFeatures'
import { editionModule } from '../lib/editionModule'

/* The rail's badges: how many of a thing wait on the viewer, polled every `PENDING_POLL_MS` as the bell is.
   `enabled` false for someone who decides nothing: they have no box to count, and read 0. A decision
   invalidates (or shares) the key, so the badge follows it between polls. `count` reads the payload
   defensively: a malformed body counts nothing. */
export const PENDING_POLL_MS = 60_000

function usePendingCount<T>({ enabled, count, ...options }: {
  queryKey: QueryKey; queryFn: () => Promise<T>; enabled: boolean; count: (data: T) => number
}): number {
  const query = useQuery({ ...options, enabled, refetchInterval: PENDING_POLL_MS })
  return enabled && query.data !== undefined ? count(query.data) : 0
}

const countField = (data: { count: number }) => (typeof data?.count === 'number' ? data.count : 0)

/** Self-service orgs awaiting approval — the Directory badge; the operator decides. */
export const usePendingOrgCount = (enabled: boolean): number =>
  usePendingCount({ queryKey: PENDING_ORG_COUNT_KEY, queryFn: getPendingOrgCount, enabled, count: countField })

/* The trust center's wire (`lib/trustRequests.ts`), loaded where present (`lib/editionModule.ts`): `trust_center`'s. */
type TrustCount = { PENDING_TRUST_REQUEST_COUNT_KEY: QueryKey; getPendingTrustRequestCount: () => Promise<{ count: number }> }
const trust = editionModule(isEnabled('trust_center'),
  () => import.meta.glob<TrustCount>('../lib/trustRequests.ts', { eager: true }))
const NO_COUNT = { count: 0 }

/** Trust document requests awaiting the operator — added to the Customers badge beside pending orgs. Never asked
 *  without the trust center. */
export const usePendingTrustRequestCount = (enabled: boolean): number => usePendingCount({
  queryKey: trust?.PENDING_TRUST_REQUEST_COUNT_KEY ?? ['trust-requests', 'none'],
  queryFn: trust?.getPendingTrustRequestCount ?? (async () => NO_COUNT),
  enabled: enabled && trust !== undefined,
  count: countField,
})

/* The support desk's wire (`lib/support.ts`), loaded where present (`lib/editionModule.ts`): `support_assistant`'s.
   Every org's open tickets is the operator's queue; its key sits under `['support-tickets']`, so acting on a ticket
   (which invalidates that prefix) refreshes the badge before the next poll. */
type OpenTickets = { scope: 'all'; status: 'open'[] }
type SupportWire = {
  countTickets: (f: OpenTickets) => Promise<{ count: number }>
  ticketCountKey: (f: OpenTickets) => QueryKey
}
const support = editionModule(isEnabled('support_assistant'),
  () => import.meta.glob<SupportWire>('../lib/support.ts', { eager: true }))
const OPEN_TICKETS: OpenTickets = { scope: 'all', status: ['open'] }

/** Open support tickets across all orgs (the global org filter applies) — the operator's Support badge. Never asked
 *  without the support desk. */
export const useOpenTicketCount = (enabled: boolean): number => usePendingCount({
  queryKey: support?.ticketCountKey(OPEN_TICKETS) ?? ['support-tickets', 'none'],
  queryFn: support ? () => support.countTickets(OPEN_TICKETS) : (async () => NO_COUNT),
  enabled: enabled && support !== undefined,
  count: countField,
})
