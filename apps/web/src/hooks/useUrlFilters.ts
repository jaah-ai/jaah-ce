import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

/* Filters that live in the URL, READ-THROUGH: no second copy, a pasteable link, and Back undoes a filter
 * (`workflows/WorkflowRunsTab`, `audit/ScheduleTab`, `audit/SlotIntentsTab`'s node lens, `audit/SessionsTab`'s
 * `?task=`). Local-state filters are `usePagedLogFilters`.
 * `setFilters` writes onto `prev`, not captured params: sibling panels own other keys on the same location.
 * A cleared key is DROPPED (an empty `?status=` is a 422). `replace`, so filter clicks do not flood history;
 * `{ push: true }` for a change Back should undo (`compliance/ReposTab`'s `?setup=1` opens setup over the list). */
export function useUrlFilters(): [URLSearchParams, (changes: Record<string, string>, options?: { push?: boolean }) => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const setFilters = useCallback((changes: Record<string, string>, options?: { push?: boolean }) => {
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      for (const [key, value] of Object.entries(changes)) {
        if (value) params.set(key, value)
        else params.delete(key)
      }
      return params
    }, { replace: !options?.push })
  }, [setSearchParams])
  return [searchParams, setFilters]
}

const SAME_KEYS = <F,>(patch: Partial<F>): Record<string, string> => patch as Record<string, string>

/** A typed view over the URL filters for one strip: `read` parses the params (a value outside its vocabulary
 *  reads as none), `patch` writes the fields it names, `clear` writes `empty`. `toParams` renames a strip's fields
 *  to its URL keys where two strips share one address (the Hosts tab's `hq`/`hstate`); both it and `read` must be
 *  module-level, or every render re-makes the setters. */
export function useTypedUrlFilters<F extends Record<string, string>>(
  read: (params: URLSearchParams) => F,
  empty: F,
  toParams: (patch: Partial<F>) => Record<string, string> = SAME_KEYS,
): [F, (patch: Partial<F>) => void, () => void] {
  const [params, setFilters] = useUrlFilters()
  const filters = useMemo(() => read(params), [params, read])
  const patch = useCallback((next: Partial<F>) => setFilters(toParams(next)), [setFilters, toParams])
  const clear = useCallback(() => setFilters(toParams(empty)), [setFilters, toParams, empty])
  return [filters, patch, clear]
}
