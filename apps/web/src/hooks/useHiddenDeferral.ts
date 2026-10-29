import { useCallback, useEffect, useRef } from 'react'

/* A HIDDEN BROWSER TAB READS NOTHING LIVE. React Query already pauses `refetchInterval` while the
 * document is hidden, but the console's SSE-driven refetches do not: a background tab on a busy fleet re-read its
 * whole view on every `task` event, and every tab behind one office NAT spends the same per-IP WAF budget
 * at the public edge. A refetch asked for while hidden is remembered instead, and
 * runs ONCE when the tab is shown again — however many events arrived meanwhile. */

export function isPageHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/** A stable `request` that runs `run` now while the page is visible, or once on its next `visibilitychange` to
 *  visible when it is hidden. `run` is read at fire time (latest closure). */
export function useHiddenDeferral(run: () => void): () => void {
  const runRef = useRef(run)
  runRef.current = run
  const pendingRef = useRef(false)

  useEffect(() => {
    const onVisibility = () => {
      if (isPageHidden() || !pendingRef.current) return
      pendingRef.current = false
      runRef.current()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  return useCallback(() => {
    if (isPageHidden()) {
      pendingRef.current = true
      return
    }
    runRef.current()
  }, [])
}
