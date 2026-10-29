import { useCallback, useEffect, useRef } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { useEventStream } from './useEventStream'
import { useLiveRefetch } from './useLiveRefetch'
import { useUrlFilters } from './useUrlFilters'

/* THE CONSOLE'S ONE NUMBERED, SERVER-PAGED LIST: page / per / sort / dir in the URL, one page read
 * per render, the server's `Page[T]` envelope (`apps/api/app/list_page.py`). `ui/ListPager` draws the footer,
 * `ui/feed/FeedList`'s `columns`/`sort`/`onSort` the sortable header. Offset lists that predate it (sessions,
 * credentials, the audit tabs) keep `usePagedLogFilters`.
 * READ-THROUGH (`useUrlFilters`): no second copy of the state, a pasteable link, `replace` so paging does not
 * flood history. Defaults are DROPPED from the URL, so a page at rest carries a clean link. */

export const PER_OPTIONS = [25, 50, 100] as const
export const PER_DEFAULT = 50

export type SortDir = 'asc' | 'desc'
export type Page<T> = { items: T[]; total: number; page: number; per: number }
export type ListPageState = { page: number; per: number; sort: string | null; dir: SortDir }

/* The server's ceiling (`list_page.py`'s `page` bound); a hand-typed `?page=` past it reads as page one rather
   than earning a 422. */
const PAGE_MAX = 100_000

function readPage(raw: string | null): number {
  if (!raw || !/^\d+$/.test(raw)) return 1
  const n = Number(raw)
  return n >= 1 && n <= PAGE_MAX ? n : 1
}

function readPer(raw: string | null): number {
  const n = Number(raw)
  return (PER_OPTIONS as readonly number[]).includes(n) ? n : PER_DEFAULT
}

/** URL-synced (useUrlFilters, replace) page/per/sort/dir; keys `${prefix}page`, `${prefix}per`, `${prefix}sort`,
 *  `${prefix}dir`; defaults are DROPPED from the URL. `resetKey`: when it changes after mount, page → 1 (pass a
 *  serialization of the filters). Values outside vocabulary (bad per, sort not in `sorts`) read as default.
 *  No `sorts` = a list with no sortable column: `sort` always reads null. */
export function useListPage(opts: { prefix?: string; sorts?: readonly string[]; resetKey?: string } = {}):
  ListPageState & { setPage(n: number): void; setPer(n: number): void; toggleSort(key: string, firstDir?: SortDir): void } {
  const { prefix = '', sorts, resetKey } = opts
  const [params, setFilters] = useUrlFilters()
  const keys = { page: `${prefix}page`, per: `${prefix}per`, sort: `${prefix}sort`, dir: `${prefix}dir` }

  const rawSort = params.get(keys.sort)
  const sort = rawSort && sorts?.includes(rawSort) ? rawSort : null
  /* `dir` means nothing without a column; reading it as 'asc' there keeps the state one shape. */
  const dir: SortDir = sort && params.get(keys.dir) === 'desc' ? 'desc' : 'asc'
  const per = readPer(params.get(keys.per))

  /* A FILTER CHANGE LANDS ON PAGE ONE IN THE SAME RENDER: the stale page is read as 1 the moment `resetKey`
     moves, and the effect then drops it from the URL — otherwise the first read under the new filters would ask
     for the old page (one wasted request, and an empty flash when the narrowed set is shorter). */
  const seenKey = useRef(resetKey)
  const staleKey = seenKey.current !== resetKey
  /* The effect's `setFilters` lands a render LATER; a render in between (a query settling, a keystroke) sees the
     key as seen and the old `?page=` still in the URL, and would read the dropped page under the new filters.
     So the dropped raw value is remembered until the URL stops carrying it. */
  const rawPage = params.get(keys.page)
  const droppedPage = useRef<string | null>(null)
  const page = staleKey || (rawPage !== null && rawPage === droppedPage.current) ? 1 : readPage(rawPage)
  useEffect(() => {
    if (seenKey.current === resetKey) return
    seenKey.current = resetKey
    droppedPage.current = rawPage
    setFilters({ [keys.page]: '' })
  }, [resetKey, rawPage, keys.page, setFilters])
  useEffect(() => {
    if (droppedPage.current !== null && rawPage !== droppedPage.current) droppedPage.current = null
  }, [rawPage])

  const setPage = useCallback((n: number) => {
    // An explicit page is the operator's: never read back as the one a filter change dropped.
    droppedPage.current = null
    setFilters({ [keys.page]: n > 1 ? String(Math.min(Math.floor(n), PAGE_MAX)) : '' })
  }, [keys.page, setFilters])

  const setPer = useCallback((n: number) => {
    setFilters({ [keys.per]: n === PER_DEFAULT ? '' : String(n), [keys.page]: '' })
  }, [keys.per, keys.page, setFilters])

  /* A new column opens at `firstDir` (a date or a count reads best newest/biggest first); the same column flips.
     Either way the order changed, so page N of the old order means nothing: page one. 'asc' is the default
     direction and is dropped like every other default. */
  const toggleSort = useCallback((key: string, firstDir: SortDir = 'asc') => {
    const next: SortDir = key === sort ? (dir === 'asc' ? 'desc' : 'asc') : firstDir
    setFilters({ [keys.sort]: key, [keys.dir]: next === 'desc' ? 'desc' : '', [keys.page]: '' })
  }, [sort, dir, keys.sort, keys.dir, keys.page, setFilters])

  return { page, per, sort, dir, setPage, setPer, toggleSort }
}

/** Appends page/per/sort/dir to `params` (always page & per; sort/dir only when sort set). */
export function setPageParams(params: URLSearchParams, state: ListPageState): URLSearchParams {
  params.set('page', String(state.page))
  params.set('per', String(state.per))
  if (state.sort) {
    params.set('sort', state.sort)
    params.set('dir', state.dir)
  }
  return params
}

const POLL_MS = 30_000
/* One shared empty array, so a caller's `useMemo` over `rows` is not re-run by every loading render. */
const EMPTY: never[] = []

/** react-query read of one page: keepPreviousData, `enabled`, optional live refetch
 *  ({ topics: string[]; entity?: string; minIntervalMs?: number } via useEventStream + useLiveRefetch, polled
 *  30s while the stream is down), `retry` passthrough. */
export function useListPageQuery<T>(opts: {
  queryKey: readonly unknown[]
  path: string
  enabled?: boolean
  live?: { topics: string[]; entity?: string; minIntervalMs?: number }
  retry?: boolean | number
}): {
  rows: T[]
  total: number | null
  loading: boolean
  fetching: boolean
  error: Error | null
  refetch: () => Promise<unknown>
  dataUpdatedAt: number
} {
  const { queryKey, path, enabled = true, live, retry } = opts
  /* A non-live list opts out of the stream (`null`): with no topics every console event (a node heartbeat
     included) would re-render the page for an event it ignores. */
  const stream = useEventStream(live ? '/api/stream' : null, live?.topics)
  const query = useQuery({
    queryKey,
    queryFn: () => get<Page<T>>(path),
    enabled,
    /* A page turn, sort or filter swaps the rows when the new page lands, never through a skeleton. */
    placeholderData: keepPreviousData,
    refetchInterval: live && stream.state === 'down' ? POLL_MS : false,
    ...(retry === undefined ? null : { retry }),
  })
  /* `cancelRefetch: false`: under a steady event stream a read longer than the throttle gap must finish, or
     every one is cancelled and the rows never move. `enabled: false` is a
     degradation (the tab is off screen) — no event re-reads it. */
  useLiveRefetch(
    stream,
    () => { if (live && enabled) void query.refetch({ cancelRefetch: false }) },
    null,
    { entity: live?.entity, minIntervalMs: live?.minIntervalMs },
  )
  return {
    rows: query.data?.items ?? EMPTY,
    total: query.data?.total ?? null,
    loading: query.isLoading,
    fetching: query.isFetching,
    error: query.isError ? query.error : null,
    refetch: query.refetch,
    dataUpdatedAt: query.dataUpdatedAt,
  }
}
