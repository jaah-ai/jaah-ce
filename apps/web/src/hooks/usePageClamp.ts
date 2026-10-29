import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'

/* `totalPages` for a 1-based server-paged list (floor 1; `per <= 0` reads as one page); a result that
 * shrinks past the last page (deletion, narrowed filter, rows ageing out) snaps back to it. */
export function usePageClamp(page: number, setPage: Dispatch<SetStateAction<number>>, total: number, per: number): number {
  const totalPages = per > 0 ? Math.max(1, Math.ceil(total / per)) : 1
  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages, setPage])
  return totalPages
}

/* Client-side paging over an in-memory list, `pageSize` per page: the 1-based `page` (clamped by `usePageClamp`),
 * the `pageItems` slice drawn, its `start` index in `items`, and the pager's "x–y of n" `label`. `current` is
 * the page actually drawn: the frame before the clamp lands (rows removed) it is the last page, not an empty one. */
export function useClientPage<T>(items: readonly T[], pageSize: number) {
  const [page, setPage] = useState(1)
  const totalPages = usePageClamp(page, setPage, items.length, pageSize)
  const current = Math.min(page, totalPages)
  const start = (current - 1) * pageSize
  const pageItems = useMemo(() => items.slice(start, start + pageSize), [items, start, pageSize])
  return { page, current, setPage, totalPages, start, pageItems, label: `${start + 1}–${Math.min(start + pageSize, items.length)} of ${items.length}` }
}
