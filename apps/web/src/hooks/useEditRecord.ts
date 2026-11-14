import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { QueryClientContext, QueryObserver, type QueryKey } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { apiDetail, ApiError } from '../api'
import { type ConfirmOptions, useConfirm } from '../components/ConfirmDialog'

/* An id-keyed edit page's load and delete — every routed edit page loads its record here (`UserEditPage`,
 * `GroupEditPage`, `ProjectEditPage`, `FeatureEditPage`, `playbooks/PlaybookEditPage`,
 * `automations/AutomationEditPage`, `credentials/AccountEditPage`, `credentials/CredentialEditPage`, …) and saves
 * through `useSubmit`. `id === null` is create mode:
 * nothing is fetched, never "loading". A new id re-shows loading; a landed load clears not-found/error.
 * `fetch`/`onLoad` ride a ref, so an inline closure does not re-fire the load. A superseded or unmounted
 * read writes NOTHING (generation fence): a late response must not re-seed the form over typing.
 * `fixedLoadError` shows `loadFallback` instead of the server's detail. `reload` re-reads without the
 * skeleton. Delete asks via `components/ConfirmDialog` (never `window.confirm`: a suppressed native dialog
 * fires without intent; the page renders `confirmDialog` once), then `replace`s to `listTo`, since this URL
 * now names a missing row; a page that deletes passes `listTo` and `deleteFallback`.
 * `cacheKey` (needs a `QueryClientProvider`) reads through react-query under that key (with `retry`), for a record
 * another surface writes: an observer on the key follows it like a `useQuery` would — a sibling's
 * `invalidateQueries`, a window refocus — and each newer successful answer calls `onLoad` again (no skeleton; a
 * failed re-read changes nothing), so an `onLoad` that must keep typing re-seeds only an untouched draft
 * (`ProjectEditPage`). A row already cached under the key — or `initialData`'s, for a key not yet cached (a list
 * row) — reaches `onLoad` at once while the read revalidates behind it, so a page that gates on its draft rather
 * than `loading` paints with no skeleton. */
/* One cached answer `onLoad` saw: its `dataUpdatedAt` and its data (see `applied` below). */
type Answer = { at: number; data: unknown }
const isNewAnswer = (last: Answer, at: number, data: unknown) =>
  at > last.at || (at === last.at && !Object.is(data, last.data))

/* Only a caller's own `retry`: react-query spreads options over the client's defaults, so the observer's
   `retry: undefined` erased the client's `retry: false` and its read retried 3 times (~7s) — the read `fetchQuery`
   then joins, so a 403 page held "Loading…". */
const retryOption = (retry: ((failureCount: number, error: unknown) => boolean) | undefined) =>
  retry === undefined ? {} : { retry }

export function useEditRecord<T>(
  id: string | null,
  fetch: (id: string) => Promise<T>,
  onLoad: (row: T) => void,
  opts: {
    loadFallback: string; fixedLoadError?: boolean; notFoundStatuses?: readonly number[]
    listTo?: string; deleteFallback?: string
    cacheKey?: (id: string) => QueryKey; retry?: (failureCount: number, error: unknown) => boolean
    initialData?: (id: string) => T | undefined
  },
) {
  const navigate = useNavigate()
  const [confirm, confirmDialog] = useConfirm()
  const [loading, setLoading] = useState(id !== null)
  const [notFound, setNotFound] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const live = useRef({ fetch, onLoad, opts })
  live.current = { fetch, onLoad, opts }
  const gen = useRef(0)
  const queryClient = useContext(QueryClientContext)
  // The cached answer `onLoad` last saw (`dataUpdatedAt` + data), so it is never handed the same one twice. The
  // stamp is `Date.now()`: a read answering within initialData's millisecond shares its stamp, so a tie is a new
  // answer unless the data is the very same.
  const applied = useRef<Answer>({ at: 0, data: undefined })

  const load = useCallback(async (key: string) => {
    const mine = ++gen.current
    const { loadFallback, fixedLoadError, notFoundStatuses = [404] } = live.current.opts
    let land: () => void
    try {
      const { cacheKey: keyOf, retry } = live.current.opts
      const queryKey = keyOf?.(key)
      let row: T
      let at = 0
      if (queryKey && queryClient) {
        const fetched = await queryClient.fetchQuery({ queryKey, queryFn: () => live.current.fetch(key), ...retryOption(retry) })
        // The cache's own copy and stamp, read together: the very reference the observer holds (structural sharing
        // makes it differ from `fetched`), and never an older row under a newer stamp.
        const state = queryClient.getQueryState<T>(queryKey)
        row = state?.status === 'success' ? state.data as T : fetched
        at = state?.status === 'success' ? state.dataUpdatedAt : 0
      } else {
        row = await live.current.fetch(key)
      }
      land = () => {
        // The observer may have handed this very answer over already: one `onLoad` per answer.
        if (!at || isNewAnswer(applied.current, at, row)) { applied.current = { at, data: row }; live.current.onLoad(row) }
        setNotFound(false); setLoadError('')
      }
    } catch (err) {
      land = err instanceof ApiError && notFoundStatuses.includes(err.status)
        ? () => { setNotFound(true); setLoadError('') }
        : () => { setNotFound(false); setLoadError(fixedLoadError ? loadFallback : apiDetail(err, loadFallback)) }
    }
    // One synchronous batch with `loading`: a caller whose `onLoad` nulls the id (`GroupEditPage`) strands nothing.
    if (mine !== gen.current) return
    try { land() } catch { setLoadError(live.current.opts.loadFallback) } finally { setLoading(false) }
  }, [queryClient])

  // Before the load below, so the key is built with `initialData` and the load's read joins the observer's.
  const cached = opts.cacheKey !== undefined
  useEffect(() => {
    const keyOf = live.current.opts.cacheKey
    if (id === null || !cached || !keyOf || !queryClient) return
    const observer = new QueryObserver(queryClient, {
      queryKey: keyOf(id), queryFn: () => live.current.fetch(id), ...retryOption(live.current.opts.retry),
      initialData: () => live.current.opts.initialData?.(id),
    })
    return observer.subscribe(result => {
      if (result.status !== 'success' || !isNewAnswer(applied.current, result.dataUpdatedAt, result.data)) return
      applied.current = { at: result.dataUpdatedAt, data: result.data }
      try { live.current.onLoad(result.data); setNotFound(false); setLoadError('') } catch { setLoadError(live.current.opts.loadFallback) }
    })
  }, [id, cached, queryClient])

  useEffect(() => {
    if (id === null) { setLoading(false); return }
    setLoading(true)
    void load(id)
    return () => { gen.current++ }
  }, [id, load])

  const reload = useCallback(async () => { if (id !== null) await load(id) }, [id, load])

  async function confirmAndDelete(options: ConfirmOptions, remove: () => Promise<unknown>) {
    if (!(await confirm(options))) return
    setDeleting(true)
    setDeleteError('')
    try {
      await remove()
      navigate(opts.listTo ?? '', { replace: true })
    } catch (err) {
      setDeleteError(apiDetail(err, opts.deleteFallback ?? ''))
    } finally {
      setDeleting(false)
    }
  }

  return { confirm, confirmDialog, loading, notFound, loadError, reload, deleting, deleteError, confirmAndDelete }
}
