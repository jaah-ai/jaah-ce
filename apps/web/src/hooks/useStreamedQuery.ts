import { type QueryKey, type UseQueryOptions, type UseQueryResult, useQuery } from '@tanstack/react-query'
import type { UseEventStream } from './useEventStream'
import { useLiveRefetch } from './useLiveRefetch'

/* The plain live query: a react-query read refetched (debounced, `useLiveRefetch`) on `stream`'s events —
   `entity`'s only, when given — and polled every `pollMs` by the query's own interval only while the stream
   is down. `enabled: false` gates the live refetch too: `enabled` does NOT stop an imperative `refetch()` in
   react-query 5. A site that invalidates a key family instead, polls for a reason of its own, or hears several
   entities at once keeps its own wiring (`JaahBalanceCard`, `WorktreePool`). */
export function useStreamedQuery<TData, TKey extends QueryKey = QueryKey>(
  stream: UseEventStream,
  { pollMs, entity }: { pollMs: number; entity?: string },
  { enabled = true, ...options }:
    Omit<UseQueryOptions<TData, Error, TData, TKey>, 'refetchInterval' | 'enabled'> & { enabled?: boolean },
): UseQueryResult<TData, Error> {
  const query = useQuery({ ...options, enabled, refetchInterval: stream.state === 'down' ? pollMs : false })
  useLiveRefetch(stream, () => { if (enabled) void query.refetch() }, null, { entity })
  return query
}
