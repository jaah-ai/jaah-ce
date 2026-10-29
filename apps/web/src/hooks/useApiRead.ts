import { useCallback, useRef, useState } from 'react'
import { keepPreviousData, type QueryKey, useQuery } from '@tanstack/react-query'

import { apiDetail } from '../api'

/**
 * The read-one-thing-from-the-API shape, on react-query: one resource's loading/error/data as one status, with
 * the cache, de-duplication and focus/interval refetch every other read in the console already has. The
 * callers that fit this are the PLAIN case; the loaders that do not (page-and-append, an edit page's seed via
 * `useEditRecord`, `AbortController` lookups via `useAsyncOptions`) keep their own wiring.
 *
 * `key` is the query key: the first element names the read (unique across the app, or shared by sites that read
 * the same thing the same way), the rest are what the read depends on — a change re-reads; so does every mount. `enabled: false` holds the read with `loading` true (skeleton, not a
 * false empty state). `refetchInterval` polls while a number, never in a hidden tab (react-query's own rule);
 * there is no focus refetch (below).
 * `apiDetail(err, fallback)` shows the server's `detail` verbatim; `fallbackMessage` is required so no generic
 * "something went wrong" appears. `reload` re-reads with `loading` true until it lands (a Retry button's
 * feedback); `refresh` re-reads without the skeleton (the rows stay while the fresh ones land). A key change
 * re-reads with `loading` true and the previous key's `data` still held — held through a failed re-read too,
 * beside its `error`; a surface that must show nothing of the previous key reads its own cache entry instead
 * (the task's compliance manifest panel).
 *
 * Never persisted (`meta.persist: false`, `queryClient.shouldPersistQuery`): these reads are page-scoped and
 * some carry a person's data, so none outlives the page in localStorage.
 */
export function useApiRead<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  fallbackMessage: string,
  { enabled = true, refetchInterval = false }: {
    enabled?: boolean
    /** A number polls; `false`/`null` does not; a function decides from the latest answer (none yet = `null`). */
    refetchInterval?: number | false | null | ((data: T | null) => number | false | null)
  } = {},
): { data: T | null; loading: boolean; error: string; reload: () => Promise<void>; refresh: () => Promise<void> } {
  const query = useQuery({
    queryKey: key,
    /* Called with NO arguments: react-query hands its `QueryFunctionContext` to the function it is given, and a
       bare reference such as `listProviders` (whose optional first parameter is an org id) would read it as one. */
    queryFn: () => fetcher(),
    enabled,
    /* Every mount re-reads (the cached answer paints at once meanwhile): a write made elsewhere must show on the
       next visit, as it did with the hand-rolled reads; siblings mounting together still share the one read. */
    staleTime: 0,
    refetchInterval: typeof refetchInterval === 'function'
      ? query => refetchInterval(query.state.data ?? null) || false
      : refetchInterval || false,
    /* No focus refetch: a page-scoped read re-reads on its key, its interval or an explicit `reload`, as the
       hand-rolled reads it replaced did — a surface that seeds local state from the answer (a pick list) must not
       have it reset by a tab switch. */
    refetchOnWindowFocus: false,
    /* A key change keeps the last answer on screen while the next one loads, with `loading` true meanwhile — the
       hand-rolled shape (a pager's total does not drop to 0 between pages; a chart stays under its skeleton). */
    placeholderData: keepPreviousData,
    meta: { persist: false },
  })
  const { refetch } = query
  /* `reload` SHOWS the read (a Retry in flight is not the previous failure): `loading` is true until it lands.
     `refresh` is the same re-read without the skeleton (a live refetch redraws near-identical rows, no flash). */
  const [reloading, setReloading] = useState(false)
  const reload = useCallback(async () => {
    setReloading(true)
    try { await refetch() } finally { setReloading(false) }
  }, [refetch])
  const refresh = useCallback(async () => { await refetch() }, [refetch])
  /* The last answer outlives a FAILED re-read too (react-query drops `data` when a new key errors): the rows stay
     under the error banner, as the hand-rolled reads kept them, and a pager's total never drops to 0 on a failure. */
  const lastAnswer = useRef<T | null>(null)
  if (query.data !== undefined) lastAnswer.current = query.data
  return {
    data: query.data ?? lastAnswer.current,
    loading: query.isPending || query.isPlaceholderData || reloading,
    error: query.error ? apiDetail(query.error, fallbackMessage) : '',
    reload,
    refresh,
  }
}
