import { useEffect, useRef } from 'react'
import { isPageHidden } from './useHiddenDeferral'

/* A fixed-interval pass over an IMPERATIVE async that is not a query's refetch (a query polls through
   `useApiRead`'s `refetchInterval`): `fn` every `intervalMs`, none while `null`. `fn` is read at fire time (latest
   closure), so the timer is keyed on the interval alone and never re-armed by a render; the first pass is the
   caller's (`useEffect`), not this hook's. One pass at a time: a tick landing while the
   previous pass is still out is dropped, so a slow read never overlaps the next — and a pass that never
   settles holds the poll for good, so `fn` must always settle. A rejected pass is the caller's to report
   (each catches its own); here it only frees the next tick.
   A HIDDEN TAB polls nothing, like React Query's `refetchInterval` (`useHiddenDeferral`): its ticks are
   skipped, and ONE catch-up pass runs when the tab is shown again — only if a tick was skipped meanwhile. A
   background tab on the queue re-read the whole fleet's queue every tick for nobody. */
export function usePoll(fn: () => unknown, intervalMs: number | null): void {
  const fnRef = useRef(fn)
  fnRef.current = fn
  const inFlight = useRef(false)
  useEffect(() => {
    if (intervalMs === null) return
    let skipped = false
    const release = () => { inFlight.current = false }
    // True when a pass started; a catch-up refused by an earlier pass still out stays owed.
    const pass = (): boolean => {
      if (inFlight.current) return false
      inFlight.current = true
      Promise.resolve().then(fnRef.current).then(release, release)
      return true
    }
    const timer = setInterval(() => {
      if (isPageHidden()) skipped = true
      else if (pass()) skipped = false
    }, intervalMs)
    const onVisibility = () => {
      if (isPageHidden() || !skipped) return
      if (pass()) skipped = false
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [intervalMs])
}
