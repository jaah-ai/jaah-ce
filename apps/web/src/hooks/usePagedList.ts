import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'
import { apiDetail } from '../api'

/* ── A "LOAD OLDER" LEDGER, ONE PAGE PER PRESS ── the state behind `ui/PagedList`.
 *
 * Not `useInfiniteQuery`: these ledgers must keep the last good rows under a failed refetch (a new key's
 * failure would drop them), `reload` restarts at page one (a refetch re-reads every loaded page), and their
 * suites step reads with `act` rather than the query client's timers.
 *
 * THE CURSOR IS THE SERVER'S, ECHOED VERBATIM (`nextOf`); `null` = no further page. Never derived from
 * `rows.length`: an offset breaks on shared timestamps.
 * `fetchPage` IS THE QUERY KEY: a new identity (memoised on the filters) restarts at page one, so a cursor
 * minted under old filters is never sent. Memoise it, or every render re-reads.
 * THE GENERATION GUARD: a filter change and a "Load older" can resolve out of order; a stale winner would
 * show the wrong rows with a cursor into the wrong list. The `finally` is fenced too, or a superseded read
 * clears the skeleton early.
 * A FAILURE KEEPS THE ROWS: the caller says the reading is stale; with no rows, `ui/PagedList` offers the
 * retry (the latch is sticky and "Load older" needs a cursor never obtained).
 * AN APPENDED PAGE SCROLLS INTO VIEW: its first row (by `feed-row` test id — a detail region is a sibling node)
 * under `bodyRef`, `block: 'nearest'`. Optional call: jsdom has no `scrollIntoView`. */

/** The page one press reads, sent as `limit` to the runs routes (`routes/workflows.py`, `routes/crons.py`,
 *  `routes/automations.py` each default to it; their `RUNS_MAX_LIMIT` is pinned above it in the ledgers' suites). */
export const RUNS_PAGE_SIZE = 50

export type PagedListState<Row, Page> = {
  rows: Row[]
  /** The newest page that landed, for what rides beside the rows (filter vocabularies, flags). */
  lastPage: Page | null
  hasMore: boolean
  loading: boolean
  error: string
  /** A first page has landed, so an empty list is an answer, not a skeleton. */
  loaded: boolean
  /** Page one again, replacing the rows. Never throws. */
  reload: () => Promise<void>
  loadMore: () => Promise<void>
  bodyRef: RefObject<HTMLDivElement | null>
}

export function usePagedList<Row, Cursor, Page>({
  fetchPage,
  rowsOf,
  nextOf,
  errorFallback,
  enabled = true,
}: {
  fetchPage: (cursor: Cursor | null) => Promise<Page>
  rowsOf: (page: Page) => Row[]
  nextOf: (page: Page) => Cursor | null
  errorFallback: string
  /** Nothing is read until true (a hidden tab). Latch it false→true, so returning does not re-read. */
  enabled?: boolean
}): PagedListState<Row, Page> {
  const [rows, setRows] = useState<Row[]>([])
  const [lastPage, setLastPage] = useState<Page | null>(null)
  const [cursor, setCursor] = useState<Cursor | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [appendedFrom, setAppendedFrom] = useState<number | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const generation = useRef(0)
  /* Read from refs: the boundary not from a state updater (StrictMode double-invokes it), the readers so
     inline lambdas are not a dependency. */
  const count = useRef(0)
  count.current = rows.length
  const read = useRef({ rowsOf, nextOf, errorFallback })
  read.current = { rowsOf, nextOf, errorFallback }

  const load = useCallback(async (from: Cursor | null) => {
    const mine = ++generation.current
    setLoading(true)
    setError('')
    /* Page one drops the old cursor: kept under a failed reload, it would append the OLD list's next page. */
    if (from === null) setCursor(null)
    try {
      const page = await fetchPage(from)
      if (mine !== generation.current) return
      const next = read.current.rowsOf(page)
      if (from !== null) setAppendedFrom(count.current)
      setRows(prev => (from !== null ? [...prev, ...next] : next))
      setCursor(read.current.nextOf(page))
      setLastPage(page)
      setLoaded(true)
    } catch (err) {
      if (mine !== generation.current) return
      setError(apiDetail(err, read.current.errorFallback))
    } finally {
      if (mine === generation.current) setLoading(false)
    }
  }, [fetchPage])

  useEffect(() => {
    if (!enabled) return
    void load(null)
  }, [enabled, load])

  useEffect(() => {
    if (appendedFrom === null) return
    setAppendedFrom(null)
    bodyRef.current
      ?.querySelectorAll('[data-testid="feed-row"]')[appendedFrom]
      ?.scrollIntoView?.({ block: 'nearest' })
  }, [appendedFrom])

  const reload = useCallback(() => load(null), [load])
  const loadMore = useCallback(() => (cursor === null ? Promise.resolve() : load(cursor)), [cursor, load])

  return { rows, lastPage, hasMore: cursor !== null, loading, error, loaded, reload, loadMore, bodyRef }
}
