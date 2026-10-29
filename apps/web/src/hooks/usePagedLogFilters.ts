import { useCallback, useMemo, useState } from 'react'
import { isoWithOffset, localInputToIso, viewerZone } from '../lib/zonedTime'

/* The filter core the paged log tabs share (Audit › Nodes, Exclusions; Sessions and the intent tables
 * take only search/page): zone, search term, From/To range, page and the pinned instant. Local state, not
 * URL (D8); each caller owns its selects and request. Audit › Activity keeps its filters in the URL and
 * pages by cursor (`pages/audit/activityFilters.ts`), taking only `PER_PAGE`. */
export const PER_PAGE = 50
/* The API's `offset` bound (`le=100_000`); past it is a 422, so Next never offers that page. */
export const OFFSET_MAX = 100_000

/** Cleared means omitted (`?start=` is a 422), and so does an unparseable bound (`fromUnparseable`). */
export function setRangeParams(params: URLSearchParams, from: string, to: string): void {
  const start = localInputToIso(from)
  if (typeof start === 'string') params.set('start', start)
  const end = localInputToIso(to)
  if (typeof end === 'string') params.set('end', end)
}

/** The pager's reach over a 0-based `page`: the last page the endpoint will seek to (`OFFSET_MAX`). */
export function logPages(total: number, page: number) {
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE))
  const maxPageIndex = Math.min(totalPages - 1, Math.floor(OFFSET_MAX / PER_PAGE))
  return { totalPages, maxPageIndex, atLastReachablePage: page >= maxPageIndex }
}

export function usePagedLogFilters(firstPage: 0 | 1 = 0) {
  const zone = useMemo(viewerZone, [])
  const [query, setQuery] = useState('')
  // Bumped by a clear so the box drops typed-but-unsent text (`ui/SearchBox`'s `resetKey`).
  const [searchReset, setSearchReset] = useState(0)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState<number>(firstPage)
  /* Re-pinned on filter change, never while paging, so rows do not shift between pages. */
  const [pinnedAt, setPinnedAt] = useState(() => isoWithOffset(new Date()))

  /* Every filter change resets the page and re-pins: page 4 of the old query is not page 4 of the new. */
  const narrow = useCallback((change: () => void) => {
    change()
    setPage(firstPage)
    setPinnedAt(isoWithOffset(new Date()))
  }, [firstPage])

  /* The shared half of Clear filters; the caller clears its own beside it, inside `narrow`. */
  const clearShared = useCallback(() => {
    setQuery('')
    setSearchReset(n => n + 1)
    setFrom('')
    setTo('')
  }, [])

  return {
    zone, query, from, to, page, setPage, pinnedAt, narrow, clearShared,
    /* Whether `clearShared` would change anything; the caller ORs in its own filters. */
    sharedFiltersSet: query !== '' || from !== '' || to !== '',
    /* Spread onto `ui/SearchBox`; `query` is the debounced value that travels. */
    searchFields: {
      resetKey: searchReset, value: query,
      onChange: (next: string) => narrow(() => setQuery(next)),
    },
    /* Spread onto `ui/DateTimeBoundFields`; the flags are derived, so they match what the request sent. */
    rangeFields: {
      from, to, fromUnparseable: localInputToIso(from) === false, toUnparseable: localInputToIso(to) === false,
      onFrom: (value: string) => narrow(() => setFrom(value)),
      onTo: (value: string) => narrow(() => setTo(value)),
    },
  }
}
