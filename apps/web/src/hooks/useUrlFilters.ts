import { useCallback } from 'react'
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
