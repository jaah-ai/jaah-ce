import { type QueryKey, useQuery } from '@tanstack/react-query'
import { REQUESTS_STALE_MS, listRequests } from '../lib/accessRequests'
import { PENDING_ORG_COUNT_KEY, getPendingOrgCount } from '../lib/orgs'
import { isEnabled } from '../lib/editionFeatures'

/* The rail's badges: how many of a thing wait on the viewer, polled every `PENDING_POLL_MS` as the bell is.
   `enabled` false for someone who decides nothing: they have no box to count, and read 0. A decision
   invalidates (or shares) the key, so the badge follows it between polls. `count` reads the payload
   defensively: a malformed body counts nothing. */
export const PENDING_POLL_MS = 60_000

function usePendingCount<T>({ enabled, count, ...options }: {
  queryKey: QueryKey; queryFn: () => Promise<T>; staleTime?: number; enabled: boolean; count: (data: T) => number
}): number {
  const query = useQuery({ ...options, enabled, refetchInterval: PENDING_POLL_MS })
  return enabled && query.data !== undefined ? count(query.data) : 0
}

const countField = (data: { count: number }) => (typeof data?.count === 'number' ? data.count : 0)

/** Access requests waiting on the caller — the Requests badge. The Pending box's own query (`pages/Requests.tsx`
 *  shares the key and its staleTime), so a decision there updates the badge. */
export const usePendingRequestCount = (enabled: boolean): number => usePendingCount({
  queryKey: ['access-requests', 'pending'], queryFn: () => listRequests('pending'), staleTime: REQUESTS_STALE_MS, enabled,
  count: rows => (Array.isArray(rows) ? rows.length : 0),
})

/** Self-service orgs awaiting approval — the Directory badge; the operator decides. */
export const usePendingOrgCount = (enabled: boolean): number =>
  usePendingCount({ queryKey: PENDING_ORG_COUNT_KEY, queryFn: getPendingOrgCount, enabled, count: countField })

/* The trust center's wire (`lib/trustRequests.ts`), loaded where present: `trust_center`'s, which the community
   edition's tree leaves out (`import.meta.glob` yields `{}` for an absent file). */
type TrustCount = { PENDING_TRUST_REQUEST_COUNT_KEY: QueryKey; getPendingTrustRequestCount: () => Promise<{ count: number }> }
const trust: TrustCount | undefined = Object.values(isEnabled('trust_center')
  ? import.meta.glob<TrustCount>('../lib/trustRequests.ts', { eager: true })
  : {})[0]
const NO_COUNT = { count: 0 }

/** Trust document requests awaiting the operator — added to the Customers badge beside pending orgs. Never asked
 *  without the trust center. */
export const usePendingTrustRequestCount = (enabled: boolean): number => usePendingCount({
  queryKey: trust?.PENDING_TRUST_REQUEST_COUNT_KEY ?? ['trust-requests', 'none'],
  queryFn: trust?.getPendingTrustRequestCount ?? (async () => NO_COUNT),
  enabled: enabled && trust !== undefined,
  count: countField,
})
