import { useEffect, useRef, useState } from 'react'
import { apiDetail } from '../api'
import { useDebouncedCallback } from './useDebouncedCallback'

/* THE SERVER-BACKED OPTION LIST behind `ui/AsyncLookup` (one value) and `ui/AsyncMultiLookup` (several): the
 * debounce, one in-flight request at a time, superseded responses DISCARDED, and honest loading and error
 * state. The component owns the box; this owns what the popup lists for the box's query. */

/* The debounce: 150ms (under ~200ms a filter still feels live), 600ms max-wait so a held key cannot starve
   the request. `hooks/useDebouncedCallback` is the app's ONE debounce. Re-exported by `ui/AsyncLookup`: every
   server-searched box (`lib/orgs.ts::useOrgSearch`) keys on the same numbers. */
export const QUERY_DEBOUNCE_MS = 150
export const QUERY_MAX_WAIT_MS = 600

export interface AsyncOptionsConfig<T> {
  /* The only server seam: given the trimmed query and an `AbortSignal`, which it must pass to
     `api(path, { signal })`. Read from a ref at request time, so an inline arrow is fine. */
  fetchOptions: (query: string, signal: AbortSignal) => Promise<T[]>
  /** The box's text, trimmed — `''` while it holds an ECHO (a picked label) rather than typing. */
  query: string
  /** Whether `query` was typed. Gates the busy state, so a `fetchOnFocus` box does not spin from mount. */
  typed: boolean
  /** Below this many characters no request is made. The EMPTY query is governed by `fetchOnFocus` alone. */
  minChars: number
  /** Treat the empty query as fetchable, so focusing the box offers a first page. */
  fetchOnFocus: boolean
  /** What an EMPTY answer to a real query reads as. A FAILED fetch never borrows this line. */
  noOptionsText: string
}

export interface AsyncOptions<T> {
  /** The rows for the box's query right now: empty while a newer query is pending. */
  shown: readonly T[]
  busy: boolean
  /** The empty-list line: the failure, the "type more" hint, or `noOptionsText`. */
  emptyText: string
  /** Whether `emptyText` is a failure (drawn in the error colour). */
  failed: boolean
  /** Call on every typed change; the debounced request reads the LATEST query. */
  scheduleQuery: () => void
  /** Call on focus: requests the empty query's first page when `fetchOnFocus` and nothing is typed. */
  requestFirstPage: () => void
  /** The rows for the query RIGHT NOW, awaited: flushes the debounce and resolves once that request lands
   *  (at once when it already has), so an Enter can act on what the box would list without a second request
   *  (`components/access/PrincipalMultiSelect`'s DM1 rule). Rejects when the request fails or a newer query
   *  supersedes it; an unfetchable query resolves empty. */
  settle: () => Promise<readonly T[]>
}

type Waiter<T> = { query: string; resolve: (rows: readonly T[]) => void; reject: (err: unknown) => void }

export function useAsyncOptions<T>({
  fetchOptions,
  query,
  typed,
  minChars,
  fetchOnFocus,
  noOptionsText,
}: AsyncOptionsConfig<T>): AsyncOptions<T> {
  /* One frozen empty array per hook: `setOptions` on the same reference bails out, so clearing costs no render. */
  const [none] = useState<readonly T[]>(() => Object.freeze([]) as readonly T[])
  const [options, setOptions] = useState<readonly T[]>(none)
  const [fetchError, setFetchError] = useState('')
  const [loading, setLoading] = useState(false)
  /** The query the three states above BELONG to; `null` means "no request is warranted". */
  const [requested, setRequested] = useState<string | null>(null)
  /** Bumped by `settle` to re-ask the same query after a failed answer (`requested` alone would not re-run). */
  const [attempt, setAttempt] = useState(0)

  /* A non-empty query needs `minChars`; the empty one only `fetchOnFocus` — two cases, so `minChars={0}`
     cannot mean two things. */
  const fetchable = query.length > 0 ? query.length >= minChars : fetchOnFocus

  const fetchRef = useRef(fetchOptions)
  fetchRef.current = fetchOptions

  const { trigger: scheduleQuery, cancel: cancelScheduled } = useDebouncedCallback(
    () => setRequested(fetchable ? query : null),
    QUERY_DEBOUNCE_MS,
    QUERY_MAX_WAIT_MS,
  )

  /* `settle`'s bookkeeping, written in the request effect as it happens (state lags a render): the answer the
     last request produced, and the callers waiting for one. */
  const answered = useRef<{ query: string; rows: readonly T[] } | { query: string; error: unknown } | null>(null)
  const waiters = useRef<Waiter<T>[]>([])
  const answerWaiters = (keep: (w: Waiter<T>) => boolean, settle: (w: Waiter<T>) => void) => {
    const done = waiters.current.filter(w => !keep(w))
    waiters.current = waiters.current.filter(keep)
    done.forEach(settle)
  }

  /* One request at a time; the controller is its identity. The cleanup aborts the previous one, and both
     tails check `signal.aborted` first so a late superseded answer writes nothing. */
  useEffect(() => {
    answered.current = null
    // A waiter for any other query will never be answered now: this request superseded it.
    answerWaiters(w => w.query === requested, w => w.reject(new Error('superseded')))
    if (requested === null) {
      setOptions(none)
      setFetchError('')
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setOptions(none)
    setFetchError('')
    setLoading(true)
    fetchRef.current(requested, controller.signal)
      .then(rows => {
        if (controller.signal.aborted) return
        const next = Array.isArray(rows) ? rows : none
        answered.current = { query: requested, rows: next }
        setOptions(next)
        setLoading(false)
        answerWaiters(w => w.query !== requested, w => w.resolve(next))
      })
      .catch(err => {
        if (controller.signal.aborted) return
        answered.current = { query: requested, error: err }
        setFetchError(apiDetail(err, 'Lookup failed'))
        setLoading(false)
        answerWaiters(w => w.query !== requested, w => w.reject(err))
      })
    return () => {
      controller.abort()
      answerWaiters(w => w.query !== requested, w => w.reject(new Error('superseded')))
    }
  }, [requested, attempt, none])

  /* Whether the state describes the box right now. `pending` is the debounce window: read BUSY, never the
     previous rows or a false "No matches". */
  const settled = requested === (fetchable ? query : null)
  const pending = typed && fetchable && !settled
  const failed = Boolean(fetchError) && settled

  const emptyText = failed
    ? `${fetchError} — retype to retry.`
    : fetchable
      ? noOptionsText
      : `Type at least ${minChars} character${minChars === 1 ? '' : 's'} to search.`

  return {
    shown: settled ? options : none,
    busy: loading || pending,
    emptyText,
    failed,
    scheduleQuery,
    /* The empty-query fetch rides focus, not the debounce (a single event). `setRequested('')` over `''` is a
       no-op. */
    requestFirstPage: () => {
      if (fetchOnFocus && !typed) setRequested('')
    },
    settle: () => {
      // The pending debounce would only ask for this same query: ask now instead.
      cancelScheduled()
      if (!fetchable) {
        setRequested(null)
        return Promise.resolve(none)
      }
      const last = answered.current
      if (last && last.query === query && 'rows' in last) return Promise.resolve(last.rows)
      // A failed answer is not a verdict: an Enter after a network blip asks again rather than re-failing.
      if (last && last.query === query) setAttempt(n => n + 1)
      setRequested(query)
      return new Promise<readonly T[]>((resolve, reject) => { waiters.current.push({ query, resolve, reject }) })
    },
  }
}
