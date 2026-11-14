import { useCallback, useEffect, useRef } from 'react'
import { isPollingPaused, onPollingResumed } from '../lib/pollGate'

/* A PAUSED PAGE READS NOTHING LIVE. React Query pauses `refetchInterval` while polling is paused (`lib/pollGate.ts`:
 * a hidden tab, an idle page, a rate-limited address), but the console's SSE-driven refetches do not: a background tab
 * on a busy fleet re-read its whole view on every `task` event, and every tab behind one office NAT spends the same
 * per-IP WAF budget at the public edge. A refetch asked for while paused is remembered instead, and runs ONCE when
 * polling resumes — however many events arrived meanwhile. */

/** A stable `request` that runs `run` now while polling may run, or once when it next resumes. `run` is read at fire
 *  time (latest closure). */
export function useHiddenDeferral(run: () => void): () => void {
  const runRef = useRef(run)
  runRef.current = run
  const pendingRef = useRef(false)

  useEffect(() => onPollingResumed(() => {
    if (!pendingRef.current) return
    pendingRef.current = false
    runRef.current()
  }), [])

  return useCallback(() => {
    if (isPollingPaused()) {
      pendingRef.current = true
      return
    }
    runRef.current()
  }, [])
}
